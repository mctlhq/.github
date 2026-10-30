// Package model is a NON-NORMATIVE, stdlib-only reference model of the MCP
// gateway identity and authorization boundary (mctlhq/.github#195). It is
// architecture evidence ([REF]), not production proof.
package model

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
)

// Clock lets tests drive time deterministically.
type Clock interface{ Now() time.Time }

// FakeClock is a manually advanced clock.
type FakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func NewFakeClock(t time.Time) *FakeClock { return &FakeClock{t: t} }
func (c *FakeClock) Now() time.Time       { c.mu.Lock(); defer c.mu.Unlock(); return c.t }
func (c *FakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

// Audience is a JWT aud claim (string or array on the wire).
type Audience []string

func (a Audience) MarshalJSON() ([]byte, error) {
	if len(a) == 1 {
		return json.Marshal(a[0])
	}
	return json.Marshal([]string(a))
}

func (a *Audience) UnmarshalJSON(b []byte) error {
	var s string
	if json.Unmarshal(b, &s) == nil {
		*a = Audience{s}
		return nil
	}
	var l []string
	if err := json.Unmarshal(b, &l); err != nil {
		return err
	}
	*a = l
	return nil
}

func (a Audience) Contains(x string) bool {
	for _, v := range a {
		if v == x {
			return true
		}
	}
	return false
}

type Actor struct {
	Sub string `json:"sub"`
}

type Claims struct {
	Iss      string   `json:"iss"`
	Aud      Audience `json:"aud"`
	Sub      string   `json:"sub"`
	Pid      string   `json:"pid"`
	Iat      int64    `json:"iat"`
	Exp      int64    `json:"exp"`
	ClientID string   `json:"client_id,omitempty"`
	Jti      string   `json:"jti,omitempty"`
	Scope    string   `json:"scope,omitempty"`
	Act      *Actor   `json:"act,omitempty"`
}

type Header struct {
	Alg string `json:"alg"`
	Kid string `json:"kid"`
	Typ string `json:"typ"`
}

// Key is an Ed25519 signing key with its published identifier.
type Key struct {
	Kid     string
	Alg     string
	Private ed25519.PrivateKey
	Public  ed25519.PublicKey
}

func KeyFromSeed(kid string, seed []byte) Key {
	priv := ed25519.NewKeyFromSeed(seed)
	return Key{Kid: kid, Alg: "EdDSA", Private: priv, Public: priv.Public().(ed25519.PublicKey)}
}

func b64(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

// Assemble builds a compact JWS from arbitrary header/claims; tests use it to
// forge alg=none / HS256 / unknown-kid tokens.
func Assemble(header, claims any, sign func(signingInput []byte) []byte) string {
	h, _ := json.Marshal(header)
	c, _ := json.Marshal(claims)
	in := b64(h) + "." + b64(c)
	return in + "." + b64(sign([]byte(in)))
}

func Sign(k Key, c Claims) string {
	return Assemble(Header{Alg: k.Alg, Kid: k.Kid, Typ: "at+jwt"}, c,
		func(in []byte) []byte { return ed25519.Sign(k.Private, in) })
}

// Signature returns the signature segment of a compact token.
func Signature(tok string) string {
	p := strings.Split(tok, ".")
	if len(p) != 3 {
		return ""
	}
	return p[2]
}

var (
	ErrMalformed       = errors.New("malformed token")
	ErrAlg             = errors.New("alg not pinned for key")
	ErrUnknownKid      = errors.New("unknown kid")
	ErrKeysUnavailable = errors.New("jwks unavailable")
	ErrSignature       = errors.New("bad signature")
	ErrIssuer          = errors.New("wrong issuer")
	ErrAudience        = errors.New("wrong audience")
	ErrExpired         = errors.New("expired")
	ErrNotYetValid     = errors.New("issued in the future")
	ErrSubPid          = errors.New("sub/pid disagreement")
)

// JWKS is the published key set. Removing a key models rotation.
type JWKS struct {
	mu   sync.RWMutex
	keys map[string]Key
	down bool
}

func NewJWKS(keys ...Key) *JWKS {
	j := &JWKS{keys: map[string]Key{}}
	for _, k := range keys {
		j.keys[k.Kid] = k
	}
	return j
}
func (j *JWKS) Add(k Key)            { j.mu.Lock(); j.keys[k.Kid] = k; j.mu.Unlock() }
func (j *JWKS) Remove(kid string)    { j.mu.Lock(); delete(j.keys, kid); j.mu.Unlock() }
func (j *JWKS) SetAvailable(ok bool) { j.mu.Lock(); j.down = !ok; j.mu.Unlock() }
func (j *JWKS) lookup(kid string) (Key, error) {
	j.mu.RLock()
	defer j.mu.RUnlock()
	if j.down {
		return Key{}, ErrKeysUnavailable
	}
	k, ok := j.keys[kid]
	if !ok {
		return Key{}, ErrUnknownKid
	}
	return k, nil
}

// Document renders an RFC 7517 / 8037 JWK Set (public parts only).
func (j *JWKS) Document() []byte {
	j.mu.RLock()
	defer j.mu.RUnlock()
	var out []map[string]string
	for _, k := range j.keys {
		out = append(out, map[string]string{"kty": "OKP", "crv": "Ed25519", "kid": k.Kid,
			"alg": k.Alg, "use": "sig", "x": b64(k.Public)})
	}
	d, _ := json.Marshal(map[string]any{"keys": out})
	return d
}

// Verifier is the shared verification rule set used by the gateway, the
// exchange endpoint and reference downstreams (design.md D2).
type Verifier struct {
	JWKS     *JWKS
	Issuer   string
	Audience string
	Clock    Clock
	Skew     time.Duration
	// LegacyIgnoreAudience reproduces mctl-api@98bbf8c verifyJWT, which never
	// looks at aud. Only used to show the pre-enforcement state.
	LegacyIgnoreAudience bool
}

func (v Verifier) ForAudience(aud string) Verifier { v.Audience = aud; return v }

func (v Verifier) Verify(raw string) (Claims, error) {
	var zero Claims
	parts := strings.Split(raw, ".")
	if len(parts) != 3 {
		return zero, ErrMalformed
	}
	hb, e1 := base64.RawURLEncoding.DecodeString(parts[0])
	cb, e2 := base64.RawURLEncoding.DecodeString(parts[1])
	sb, e3 := base64.RawURLEncoding.DecodeString(parts[2])
	if e1 != nil || e2 != nil || e3 != nil {
		return zero, ErrMalformed
	}
	var h Header
	if json.Unmarshal(hb, &h) != nil {
		return zero, ErrMalformed
	}
	if h.Alg != "EdDSA" { // alg is pinned; none/HS256/anything else is refused first
		return zero, fmt.Errorf("%w: %q", ErrAlg, h.Alg)
	}
	key, err := v.JWKS.lookup(h.Kid)
	if err != nil {
		return zero, err
	}
	if key.Alg != h.Alg {
		return zero, ErrAlg
	}
	if !ed25519.Verify(key.Public, []byte(parts[0]+"."+parts[1]), sb) {
		return zero, ErrSignature
	}
	var c Claims
	if json.Unmarshal(cb, &c) != nil {
		return zero, ErrMalformed
	}
	if c.Iss != v.Issuer {
		return zero, ErrIssuer
	}
	if !v.LegacyIgnoreAudience && !c.Aud.Contains(v.Audience) {
		return zero, ErrAudience
	}
	now := v.Clock.Now()
	if !now.Before(time.Unix(c.Exp, 0).Add(v.Skew)) {
		return zero, ErrExpired
	}
	if time.Unix(c.Iat, 0).After(now.Add(v.Skew)) {
		return zero, ErrNotYetValid
	}
	if !strings.HasPrefix(c.Pid, "prn_") || (strings.HasPrefix(c.Sub, "prn_") && c.Sub != c.Pid) {
		return zero, ErrSubPid
	}
	return c, nil
}
