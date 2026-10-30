package model

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
)

type PrincipalFixture struct {
	Login   string            `json:"login"`
	Tenants map[string]string `json:"tenants"`
}

type Fixtures struct {
	GatewayPrincipal string
	Principals       map[string]PrincipalFixture
	DownstreamTools  map[string]map[string]ToolSpec
	Ceiling          map[string][]string
	Grants           []Grant
	PolicyVersion    int64
	Catalogues       map[string][]byte
	MalformedTG      []byte
	Keys             []Key
}

func readJSON(dir, name string, v any) error {
	b, err := os.ReadFile(filepath.Join(dir, name))
	if err != nil {
		return err
	}
	return json.Unmarshal(b, v)
}

func LoadFixtures(dir string) (*Fixtures, error) {
	f := &Fixtures{Catalogues: map[string][]byte{}}
	var p struct {
		GatewayPrincipal string                         `json:"gateway_principal"`
		Principals       map[string]PrincipalFixture    `json:"principals"`
		DownstreamTools  map[string]map[string]ToolSpec `json:"downstream_tools"`
	}
	if err := readJSON(dir, "principals.json", &p); err != nil {
		return nil, err
	}
	f.GatewayPrincipal, f.Principals, f.DownstreamTools = p.GatewayPrincipal, p.Principals, p.DownstreamTools
	if err := readJSON(dir, "ceiling.json", &f.Ceiling); err != nil {
		return nil, err
	}
	var g struct {
		Version int64   `json:"version"`
		Grants  []Grant `json:"grants"`
	}
	if err := readJSON(dir, "grants.json", &g); err != nil {
		return nil, err
	}
	f.Grants, f.PolicyVersion = g.Grants, g.Version
	for _, n := range []string{"api", "tg"} {
		b, err := os.ReadFile(filepath.Join(dir, "catalogues", n+".json"))
		if err != nil {
			return nil, err
		}
		f.Catalogues[n] = b
	}
	b, err := os.ReadFile(filepath.Join(dir, "catalogues", "tg-malformed.json"))
	if err != nil {
		return nil, err
	}
	f.MalformedTG = b
	var k struct {
		Keys []struct {
			Kid     string `json:"kid"`
			SeedHex string `json:"seed_hex"`
		} `json:"keys"`
	}
	if err := readJSON(dir, "keys.json", &k); err != nil {
		return nil, err
	}
	for _, e := range k.Keys {
		seed, err := hex.DecodeString(e.SeedHex)
		if err != nil {
			return nil, err
		}
		f.Keys = append(f.Keys, KeyFromSeed(e.Kid, seed))
	}
	return f, nil
}

// Snapshot builds the policy document served by the policy source.
func (f *Fixtures) Snapshot() Snapshot {
	m := map[string]map[string]string{}
	for pid, p := range f.Principals {
		m[pid] = p.Tenants
	}
	return Snapshot{Version: f.PolicyVersion, Ceiling: f.Ceiling, Grants: f.Grants, Membership: m, Groups: map[string][]string{}}
}
