#!/usr/bin/env python3
"""Agent-client compatibility probe (mctlhq/.github#290, slice #340).

Walks a step ladder against an MCP endpoint for each agent profile in
agents.json and reports the step reached and the first failing response.
Standard library only. The bearer token is read from MCTL_PROBE_TOKEN only;
there is deliberately no flag for it. No request reaches the registration
endpoint unless --register is given. No cookies are kept.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

TOKEN_ENV = "MCTL_PROBE_TOKEN"
REDACTED = "[REDACTED]"

# Verdict thresholds live here so a later change is local (owner confirms on #290).
PASS_READY = 8
SAME_STEP_MAJORITY = 7
AUTH_ONLY_MIN = 3
NOT_A_FIT_BELOW = 4
MCTL_SIDE_STEPS = ("1a", "1b", "1c", "2", "3")
AUTH_STEPS = ("1b", "1c", "1d")
PASS_STEPS = ("1a", "1b", "1d", "2", "3")

LADDER = ("1a", "1b", "1c", "1d", "2", "3")
ALL_STEPS = LADDER + ("4",)

TRANSPORTS = {"streamable-http", "sse", "stdio-only", "unknown"}
AUTHS = {"oauth-dcr", "oauth-cimd", "oauth-unspecified", "oauth-preregistered",
         "static-header", "device-code", "none", "unknown"}
REDIRECTS = {"loopback", "custom-scheme", "hosted", "unknown"}
BOOLS = {True, False, "unknown"}
RUNS = {"local", "cloud", "unknown"}
FIELD_ENUMS = {"transport": TRANSPORTS, "auth": AUTHS, "redirect_uri_form": REDIRECTS,
               "surfaces_prompts": BOOLS, "runs_where": RUNS}
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class ProfileError(ValueError):
    pass


def load_profiles(path=None):
    path = path or os.path.join(HERE, "agents.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ProfileError("agents list missing or empty")
    for a in agents:
        name = a.get("name")
        if not name or not a.get("doc_url"):
            raise ProfileError("profile without name or doc_url: %r" % (name,))
        for field, allowed in FIELD_ENUMS.items():
            cell = a.get(field)
            if not isinstance(cell, dict) or "value" not in cell:
                raise ProfileError("%s: field %s missing" % (name, field))
            if cell["value"] not in allowed:
                raise ProfileError("%s: %s=%r outside enum" % (name, field, cell["value"]))
            if cell["value"] != "unknown" and not cell.get("doc_url"):
                raise ProfileError("%s: %s has a value but no doc_url" % (name, field))
        if a["auth"]["value"] == "unknown" and not a.get("untested_reason"):
            raise ProfileError("%s: auth unknown requires untested_reason" % name)
    return data


def is_untested(profile):
    return profile["auth"]["value"] == "unknown" or profile["transport"]["value"] in ("stdio-only", "sse")


def untested_reason(profile):
    if profile["transport"]["value"] in ("stdio-only", "sse"):
        return "transport %s cannot be probed over streamable HTTP" % profile["transport"]["value"]
    return profile.get("untested_reason") or "auth method unknown"


# ---------------------------------------------------------------- redaction

_SECRET_KEYS = ("access_token", "refresh_token", "client_secret", "code", "code_verifier")
_KV_RE = re.compile(
    r'(?<![A-Za-z0-9_])(["\']?(?:%s)["\']?\s*[:=]\s*["\']?)([^"\'&\s,}]+)' % "|".join(_SECRET_KEYS))
_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)


def _redact_str(s, token):
    if token:
        s = s.replace(token, REDACTED)
    s = _BEARER_RE.sub(lambda m: m.group(1) + REDACTED, s)
    return _KV_RE.sub(lambda m: m.group(1) + REDACTED, s)


def redact(obj, token=None):
    if isinstance(obj, str):
        return _redact_str(obj, token)
    if isinstance(obj, dict):
        return {k: (REDACTED if k in _SECRET_KEYS else redact(v, token)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, token) for v in obj]
    return obj


# --------------------------------------------------------------------- HTTP

def check_endpoint(endpoint):
    u = urllib.parse.urlparse(endpoint)
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")):
        return
    raise ProfileError("endpoint must be https (http only for loopback): %s" % endpoint)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


USER_AGENT = "mctl-compat-probe/1 (+https://github.com/mctlhq/.github/issues/290)"


def make_http(timeout=15):
    # Built by hand: build_opener() always adds FileHandler, FTPHandler and
    # DataHandler, so a discovered file:// URL would be read from local disk.
    # Only http(s) is reachable here; no cookie processor, so nothing persists.
    opener = urllib.request.OpenerDirector()
    for h in (urllib.request.UnknownHandler(), urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(), _NoRedirect(),
              urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        opener.add_handler(h)
    # Cloudflare in front of api.mctl.ai answers the default "Python-urllib/x"
    # agent with 403 error 1010 before the request reaches mctl, which would
    # read as a failed step 1a. Identify the probe honestly instead.
    opener.addheaders = [("User-Agent", USER_AGENT)]

    def http(method, url, headers=None, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            resp = e
        except (urllib.error.URLError, OSError) as e:
            return {"status": 0, "headers": {}, "body": "transport error: %s" % e}
        try:
            text = resp.read().decode("utf-8", "replace")
        except OSError as e:
            text = "read error: %s" % e
        return {"status": resp.status if hasattr(resp, "status") else resp.code,
                "headers": {k.lower(): v for k, v in resp.headers.items()}, "body": text}
    return http


def parse_body(resp):
    """JSON body, or the first JSON `data:` message of an event stream."""
    text = resp.get("body") or ""
    if "text/event-stream" in resp.get("headers", {}).get("content-type", ""):
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    continue
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _evidence(resp):
    ev = {"status": resp["status"], "body": (resp.get("body") or "")[:1500]}
    wa = resp.get("headers", {}).get("www-authenticate")
    if wa:
        ev["www_authenticate"] = wa
    return ev


# ------------------------------------------------------------------ ladder

def _step(status, evidence=None):
    return {"status": status, "evidence": evidence or {}}


def _rpc(http, url, method, params, headers, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    h.update(headers)
    resp = http("POST", url, h, body)
    return resp, parse_body(resp)


def _tool_text(msg):
    res = (msg or {}).get("result") or {}
    if res.get("isError"):
        return None, None
    if isinstance(res.get("structuredContent"), (dict, list)):
        return res["structuredContent"], json.dumps(res["structuredContent"])
    text = "".join(c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict))
    try:
        return json.loads(text), text
    except ValueError:
        return None, text


def _skill_list(parsed):
    if isinstance(parsed, dict):
        parsed = parsed.get("skills")
    return parsed if isinstance(parsed, list) else []


def _metadata_ok(auth, as_meta):
    if auth == "oauth-dcr":
        return bool(as_meta.get("registration_endpoint"))
    if auth == "oauth-cimd":
        return as_meta.get("client_id_metadata_document_supported") is True
    if auth == "oauth-unspecified":
        return bool(as_meta.get("registration_endpoint")) or \
            as_meta.get("client_id_metadata_document_supported") is True
    if auth == "oauth-preregistered":
        return bool(as_meta.get("authorization_endpoint") and as_meta.get("token_endpoint"))
    return True  # static-header, device-code, none: judged at 1d


def _auth_path_ok(auth, as_meta, prm):
    """Whether the advertised metadata offers the auth path the profile needs (1d)."""
    if auth == "device-code":
        return DEVICE_GRANT in (as_meta.get("grant_types_supported") or [])
    if auth == "static-header":
        return "header" in (prm.get("bearer_methods_supported") or [])
    return True


_REDIRECTS = {"loopback": "http://127.0.0.1:8976/callback", "custom-scheme": "mctl-probe://callback",
              "hosted": "https://probe.invalid/callback"}


def run_profile(profile, endpoint, token, register, http):
    steps = {s: _step("not run") for s in ALL_STEPS}
    auth = profile["auth"]["value"]
    row = {"name": profile["name"], "version": profile.get("version_documented", "unknown"),
           "transport": profile["transport"]["value"], "auth": auth, "steps": steps,
           "untested": False, "untested_reason": None, "step_reached": None,
           "failed_step": None, "first_failure": None, "mctl_change": None}

    def finish(failed=None, evidence=None):
        passed = [s for s in LADDER if steps[s]["status"] == "pass"]
        row["step_reached"] = passed[-1] if passed else None
        if failed:
            row["failed_step"] = failed
            row["first_failure"] = evidence
            row["mctl_change"] = mctl_change(failed, profile)
        return row

    if is_untested(profile):
        row["untested"] = True
        row["untested_reason"] = untested_reason(profile)
        for s in ALL_STEPS:
            steps[s] = _step("UNTESTED")
        return finish()

    # 1a transport
    init = {"protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "mctl-compat-probe", "version": "0"}}
    resp, _ = _rpc(http, endpoint, "initialize", init, {})
    wa = resp.get("headers", {}).get("www-authenticate", "")
    m = re.search(r'resource_metadata="([^"]+)"', wa)
    if resp["status"] == 401 and m:
        steps["1a"] = _step("pass", {"status": 401, "resource_metadata": m.group(1)})
    else:
        steps["1a"] = _step("fail", _evidence(resp))
        return finish("1a", steps["1a"]["evidence"])

    def unsafe(url):
        try:
            check_endpoint(url)
            return None
        except ProfileError as e:
            return _step("fail", {"status": None, "reason": "discovered URL refused: %s" % e})

    # 1b discovery
    bad = unsafe(m.group(1))
    if bad:
        steps["1b"] = bad
        return finish("1b", bad["evidence"])
    prm_resp = http("GET", m.group(1), {"Accept": "application/json"}, None)
    prm = parse_body(prm_resp)
    if prm_resp["status"] != 200 or not isinstance(prm, dict) or not prm.get("authorization_servers"):
        steps["1b"] = _step("fail", _evidence(prm_resp))
        return finish("1b", steps["1b"]["evidence"])
    issuer = str(prm["authorization_servers"][0]).rstrip("/")
    bad = unsafe(issuer)
    if bad:
        steps["1b"] = bad
        return finish("1b", bad["evidence"])
    as_resp = http("GET", issuer + "/.well-known/oauth-authorization-server", {"Accept": "application/json"}, None)
    as_meta = parse_body(as_resp)
    if as_resp["status"] != 200 or not isinstance(as_meta, dict) or not _metadata_ok(auth, as_meta):
        steps["1b"] = _step("fail", _evidence(as_resp))
        return finish("1b", steps["1b"]["evidence"])
    steps["1b"] = _step("pass", {"registration_endpoint": bool(as_meta.get("registration_endpoint")),
                                 "cimd": as_meta.get("client_id_metadata_document_supported") is True})

    # 1c registration (DCR only, only with --register)
    dcr = auth in ("oauth-dcr", "oauth-unspecified") and as_meta.get("registration_endpoint")
    if auth == "oauth-dcr" or (auth == "oauth-unspecified" and dcr):
        if not register:
            steps["1c"] = _step("not run (--register off)")
        else:
            form = profile["redirect_uri_form"]["value"]
            redirect = _REDIRECTS.get(form, _REDIRECTS["loopback"])
            reg_body = {"client_name": "mctl-compat-probe", "redirect_uris": [redirect],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"], "token_endpoint_auth_method": "none"}
            bad = unsafe(str(as_meta["registration_endpoint"]))
            if bad:
                steps["1c"] = bad
                return finish("1c", bad["evidence"])
            r = http("POST", as_meta["registration_endpoint"], {"Content-Type": "application/json"}, reg_body)
            parsed = parse_body(r)
            if r["status"] == 201 and isinstance(parsed, dict) and parsed.get("client_id"):
                steps["1c"] = _step("pass", {"status": 201, "client_id": parsed["client_id"]})
            else:
                steps["1c"] = _step("fail", _evidence(r))
                return finish("1c", steps["1c"]["evidence"])
    else:
        steps["1c"] = _step("n/a")

    # 1d token
    if not token:
        for s in ("1d", "2", "3", "4"):
            steps[s] = _step("not run (no token)")
        return finish()
    if not _auth_path_ok(auth, as_meta, prm):
        steps["1d"] = _step("fail", {"reason": "auth path %s not advertised by metadata" % auth})
        return finish("1d", steps["1d"]["evidence"])
    bearer = {"Authorization": "Bearer " + token}
    resp, msg = _rpc(http, endpoint, "initialize", init, bearer)
    if resp["status"] == 200 and isinstance(msg, dict) and (msg.get("result") or {}).get("serverInfo"):
        steps["1d"] = _step("pass", {"status": 200})
    else:
        steps["1d"] = _step("fail", _evidence(resp))
        return finish("1d", steps["1d"]["evidence"])
    session = resp["headers"].get("mcp-session-id")
    if session:
        bearer["Mcp-Session-Id"] = session
    http("POST", endpoint, dict({"Content-Type": "application/json"}, **bearer),
         {"jsonrpc": "2.0", "method": "notifications/initialized"})

    # 2 list skills
    resp, msg = _rpc(http, endpoint, "tools/call",
                     {"name": "mctl_list_platform_skills", "arguments": {}}, bearer, 2)
    parsed, _ = _tool_text(msg)
    skills = _skill_list(parsed)
    if resp["status"] != 200 or not skills:
        steps["2"] = _step("fail", _evidence(resp))
        for s in ("3", "4"):
            steps[s] = _step("not run (earlier step failed)")
        return finish("2", steps["2"]["evidence"])
    steps["2"] = _step("pass", {"skills": len(skills)})

    # 3 read one skill
    first = skills[0]
    skill_name = first.get("name") if isinstance(first, dict) else first
    resp, msg = _rpc(http, endpoint, "tools/call",
                     {"name": "mctl_read_platform_skill", "arguments": {"skill_name": skill_name}}, bearer, 3)
    _, text = _tool_text(msg)
    if resp["status"] != 200 or not (text or "").strip():
        steps["3"] = _step("fail", _evidence(resp))
        steps["4"] = _step("not run (earlier step failed)")
        return finish("3", steps["3"]["evidence"])
    steps["3"] = _step("pass", {"skill": skill_name})

    # 4 prompt (optional, reported separately; never sets failed_step)
    if profile["surfaces_prompts"]["value"] is False:
        steps["4"] = _step("n/a")
        return finish()
    resp, msg = _rpc(http, endpoint, "prompts/list", {}, bearer, 4)
    names = [p.get("name") for p in ((msg or {}).get("result") or {}).get("prompts") or []]
    if "platform-skill" not in names:
        steps["4"] = _step("fail", _evidence(resp))
        return finish()
    resp, msg = _rpc(http, endpoint, "prompts/get", {"name": "platform-skill", "arguments": {}}, bearer, 5)
    if resp["status"] == 200 and ((msg or {}).get("result") or {}).get("messages"):
        steps["4"] = _step("pass")
    else:
        steps["4"] = _step("fail", _evidence(resp))
    return finish()


def mctl_change(failed_step, profile):
    auth = profile["auth"]["value"]
    return {
        "1a": "serve the MCP endpoint at the documented path with a 401 resource_metadata challenge",
        "1b": {"oauth-dcr": "advertise registration_endpoint in authorization server metadata",
               "oauth-cimd": "advertise client_id_metadata_document_supported"}.get(
                   auth, "publish protected-resource and authorization server metadata"),
        "1c": "accept dynamic client registration for this client's redirect URI form",
        "1d": {"device-code": "device authorization grant not advertised",
               "static-header": "static bearer header not an advertised auth path"}.get(
                   auth, "accept the client's bearer token on /mcp"),
        "2": "return a non-empty list from mctl_list_platform_skills",
        "3": "return a non-empty body from mctl_read_platform_skill",
    }.get(failed_step)


# ----------------------------------------------------------------- verdict

def _status(row, step):
    v = (row.get("steps") or {}).get(step)
    return v.get("status") if isinstance(v, dict) else v


def _passed(row):
    if row.get("untested"):
        return False
    return all(_status(row, s) == "pass" for s in PASS_STEPS)


def verdict(results):
    """Pure: (outcome, rule) from rows of {"steps": {step: status}, "failed_step", "untested"}."""
    rows = list(results)
    passing = sum(1 for r in rows if _passed(r))
    if passing >= PASS_READY:
        return "PLATFORM_READY", "%d rows pass steps 1-3" % passing
    counts = {}
    for r in rows:
        f = r.get("failed_step")
        if f in MCTL_SIDE_STEPS and not r.get("untested"):
            counts[f] = counts.get(f, 0) + 1
    for step in sorted(counts):
        if counts[step] >= SAME_STEP_MAJORITY:
            return "FIX_PROTOCOL_FIRST", "%d rows fail at mctl-side step %s" % (counts[step], step)
    auth_only = sum(1 for r in rows if r.get("failed_step") in AUTH_STEPS and not r.get("untested")
                    and _status(r, "1a") == "pass")
    if auth_only >= AUTH_ONLY_MIN:
        return "AUTH_PATH_PER_AGENT", "%d rows fail only at 1b-1d with 1a green" % auth_only
    if passing < NOT_A_FIT_BELOW:
        return "NOT_A_FIT", "only %d rows pass steps 1-3" % passing
    return "NO_RULE_MATCHED", "%d rows pass; no rule matched" % passing


# ------------------------------------------------------------------ output

def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(rows):
    out = ["| agent | version | transport | auth | step reached | failing response | what mctl would change | step 4 |",
           "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        if r["untested"]:
            reached = "UNTESTED (%s)" % r["untested_reason"]
        else:
            reached = r["step_reached"] or "none"
            pending = [s for s in ALL_STEPS if r["steps"][s]["status"].startswith("not run")]
            if pending and not r["failed_step"]:
                reached += " (%s: %s)" % (",".join(pending), r["steps"][pending[0]]["status"])
        fail = json.dumps(r["first_failure"], sort_keys=True) if r["first_failure"] else ""
        step4 = r["steps"]["4"]["status"]
        out.append("| " + " | ".join(_cell(x) for x in (
            r["name"], r["version"], r["transport"], r["auth"], reached, fail,
            r["mctl_change"] or "", step4)) + " |")
    return "\n".join(out)


def main(argv=None, http=None, environ=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--profiles", default=os.path.join(HERE, "agents.json"))
    ap.add_argument("--only")
    ap.add_argument("--register", action="store_true",
                    help="send dynamic client registration requests (off by default)")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--timeout", type=float, default=15)
    args = ap.parse_args(argv)
    token = (environ if environ is not None else os.environ).get(TOKEN_ENV) or None
    try:
        check_endpoint(args.endpoint)
        profiles = load_profiles(args.profiles)["agents"]
    except (ProfileError, OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if args.only:
        profiles = [p for p in profiles if p["name"] == args.only]
        if not profiles:
            print("error: no profile named %s" % args.only, file=sys.stderr)
            return 2
    http = http or make_http(args.timeout)
    rows = [run_profile(p, args.endpoint, token, args.register, http) for p in profiles]
    if args.markdown:
        print(redact(render_markdown(rows), token))
    else:
        print(json.dumps(redact(rows, token), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
