import copy
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import probe  # noqa: E402
from fake_server import FakeServer, TOKEN  # noqa: E402

REPO = os.path.abspath(os.path.join(ROOT, os.pardir, os.pardir, os.pardir))
REPORT = os.path.join(REPO, "docs", "spikes", "agent-client-compatibility.md")
DOC = "https://example.invalid/doc"


def profile(auth="oauth-dcr", transport="streamable-http", prompts="unknown"):
    cell = lambda v: {"value": v, "doc_url": DOC}
    return {"name": "ref", "doc_url": DOC, "version_documented": "unknown",
            "transport": cell(transport), "auth": cell(auth), "redirect_uri_form": cell("loopback"),
            "surfaces_prompts": cell(prompts), "runs_where": cell("unknown"), "untested_reason": None}


def run(server, token=None, register=False, **kw):
    return probe.run_profile(profile(**kw), server.endpoint, token, register, probe.make_http(5))


def statuses(row):
    return {s: row["steps"][s]["status"] for s in probe.ALL_STEPS}


class Ladder(unittest.TestCase):
    def test_good_reaches_step3_and_4(self):  # T1
        with FakeServer("good") as s:
            r = run(s, TOKEN)
        self.assertEqual(r["step_reached"], "3")
        self.assertIsNone(r["failed_step"])
        self.assertEqual(r["steps"]["4"]["status"], "pass")
        self.assertEqual(r["steps"]["2"]["status"], "pass")

    def test_probe_sends_its_own_user_agent(self):
        with FakeServer("good") as s:
            run(s, TOKEN)
        self.assertTrue(s.user_agents)
        self.assertEqual(set(s.user_agents), {probe.USER_AGENT})

    def test_wrong_path_fails_1a_verbatim(self):  # T2
        with FakeServer("wrong_path") as s:
            r = run(s, TOKEN)
        self.assertEqual(r["failed_step"], "1a")
        self.assertEqual(r["first_failure"]["status"], 404)
        self.assertIn("not_found", r["first_failure"]["body"])
        self.assertEqual(r["steps"]["1b"]["status"], "not run")

    def test_1a_requires_401_status_not_just_header(self):
        def stub(method, url, headers=None, body=None):
            return {"status": 403, "body": "forbidden",
                    "headers": {"www-authenticate": 'Bearer resource_metadata="http://x/prm"'}}
        r = probe.run_profile(profile(), "http://127.0.0.1:1/mcp", None, False, stub)
        self.assertEqual(r["failed_step"], "1a")
        self.assertEqual(r["first_failure"]["status"], 403)

    def test_discovered_file_url_refused(self):
        calls = []

        def stub(method, url, headers=None, body=None):
            calls.append(url)
            return {"status": 401, "body": "",
                    "headers": {"www-authenticate": 'Bearer resource_metadata="file:///etc/passwd"'}}
        r = probe.run_profile(profile(), "http://127.0.0.1:1/mcp", None, False, stub)
        self.assertEqual(r["failed_step"], "1b")
        self.assertEqual(calls, ["http://127.0.0.1:1/mcp"])
        self.assertIn("refused", r["first_failure"]["reason"])

    def _discovery_stub(self, prm, as_meta):
        calls = []

        def stub(method, url, headers=None, body=None):
            calls.append(url)
            if url.endswith("/mcp"):
                return {"status": 401, "body": "", "headers": {
                    "www-authenticate": 'Bearer resource_metadata="https://127.0.0.1/prm"'}}
            if url.endswith("/prm"):
                return {"status": 200, "body": json.dumps(prm), "headers": {}}
            return {"status": 200, "body": json.dumps(as_meta), "headers": {}}
        return calls, stub

    def test_discovered_issuer_file_url_refused(self):
        calls, stub = self._discovery_stub({"authorization_servers": ["file:///etc"]}, {})
        r = probe.run_profile(profile(), "http://127.0.0.1:1/mcp", None, False, stub)
        self.assertEqual(r["failed_step"], "1b")
        self.assertEqual(len(calls), 2)
        self.assertIn("refused", r["first_failure"]["reason"])

    def test_discovered_registration_file_url_refused(self):
        meta = json.load(open(os.path.join(ROOT, "fixtures", "as-metadata.json")))
        meta["registration_endpoint"] = "file:///etc/passwd"
        calls, stub = self._discovery_stub({"authorization_servers": ["https://127.0.0.1"]}, meta)
        r = probe.run_profile(profile(), "http://127.0.0.1:1/mcp", None, True, stub)
        self.assertEqual(r["failed_step"], "1c")
        self.assertEqual(len(calls), 3)
        self.assertIn("refused", r["first_failure"]["reason"])

    def test_opener_has_no_local_scheme_handlers(self):
        http = probe.make_http(1)
        for url in ("file:///etc/passwd", "data:,secret", "ftp://127.0.0.1/x"):
            r = http("GET", url)
            self.assertEqual(r["status"], 0, url)
            self.assertIn("unknown url type", r["body"], url)

    def test_bad_bearer_fails_1d_and_redacts(self):  # T3
        with FakeServer("bad_bearer") as s:
            r = run(s, TOKEN)
            self.assertEqual(r["failed_step"], "1d")
            self.assertIn("invalid_token", r["first_failure"]["www_authenticate"])
            self.assertEqual(r["steps"]["2"]["status"], "not run")
            out = json.dumps(probe.redact([r], TOKEN))
            md = probe.redact(probe.render_markdown([r]), TOKEN)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(TOKEN, md)

    def test_no_dcr(self):  # T4
        with FakeServer("no_dcr") as s:
            dcr = run(s, TOKEN, auth="oauth-dcr")
            cimd = run(s, TOKEN, auth="oauth-cimd")
        self.assertEqual(dcr["failed_step"], "1b")
        self.assertEqual(cimd["steps"]["1b"]["status"], "pass")
        self.assertEqual(cimd["step_reached"], "3")

    def test_empty_skills_fails_2(self):  # T5
        with FakeServer("empty_skills") as s:
            r = run(s, TOKEN)
        self.assertEqual(r["failed_step"], "2")
        self.assertEqual(r["steps"]["3"]["status"], "not run (earlier step failed)")

    def test_no_prompt_fails_only_step4(self):  # T6
        with FakeServer("no_prompt") as s:
            r = run(s, TOKEN)
        self.assertEqual(r["step_reached"], "3")
        self.assertIsNone(r["failed_step"])
        self.assertEqual(r["steps"]["4"]["status"], "fail")

    def test_surfaces_prompts_false_skips_step4(self):
        with FakeServer("good") as s:
            r = run(s, TOKEN, prompts=False)
        self.assertEqual(r["steps"]["4"]["status"], "n/a")

    def test_no_token_not_run(self):  # T7
        with FakeServer("good") as s:
            r = run(s)
            calls = [b for m, p, b in s.requests if isinstance(b, dict) and b.get("method") == "tools/call"]
        self.assertEqual(statuses(r)["1d"], "not run (no token)")
        for st in ("1d", "2", "3", "4"):
            self.assertEqual(r["steps"][st]["status"], "not run (no token)")
        self.assertIsNone(r["failed_step"])
        self.assertEqual(r["step_reached"], "1b")
        self.assertEqual(calls, [])

    def test_register_gate(self):  # T8
        with FakeServer("good") as s:
            off = run(s)
            self.assertNotIn("/oauth/register", s.paths())
            self.assertEqual(off["steps"]["1c"]["status"], "not run (--register off)")
            on = run(s, register=True)
            self.assertEqual(s.paths("POST").count("/oauth/register"), 1)
        self.assertEqual(on["steps"]["1c"]["status"], "pass")
        self.assertEqual(on["steps"]["1c"]["evidence"]["client_id"], "client-xyz")

    def test_unsupported_auth_path_fails_1d_without_token_request(self):  # T9
        for auth in ("device-code", "static-header"):
            with FakeServer("good") as s:
                r = run(s, TOKEN, auth=auth)
                bearer_posts = [1 for m, p, b in s.requests if m == "POST" and p == "/mcp"]
            self.assertEqual(r["failed_step"], "1d", auth)
            self.assertEqual(len(bearer_posts), 1, "only the unauthenticated 1a request")
            self.assertIn("not", r["mctl_change"])
        self.assertIn("device authorization grant", probe.mctl_change("1d", profile("device-code")))

    def test_untested_profile_sends_nothing(self):
        p = profile("unknown")
        p["untested_reason"] = "docs silent"
        with FakeServer("good") as s:
            r = probe.run_profile(p, s.endpoint, TOKEN, True, probe.make_http(5))
            self.assertEqual(s.requests, [])
        self.assertTrue(r["untested"])
        self.assertIn("UNTESTED", probe.render_markdown([r]))

    def test_no_cookie_header_sent(self):
        seen = []
        real = probe.make_http(5)

        def spy(method, url, headers=None, body=None):
            seen.append({k.lower() for k in (headers or {})})
            return real(method, url, headers, body)
        with FakeServer("good") as s:
            probe.run_profile(profile(), s.endpoint, TOKEN, False, spy)
        self.assertTrue(all("cookie" not in h for h in seen))


class Cli(unittest.TestCase):
    def test_no_token_flag_and_env_only(self):
        with FakeServer("good") as s:
            with self.assertRaises(SystemExit):
                probe.main(["--endpoint", s.endpoint, "--token", "x"])

    def test_remote_http_endpoint_refused(self):
        self.assertEqual(probe.main(["--endpoint", "http://example.com/mcp"]), 2)

    def test_markdown_has_12_rows_and_json_default(self):
        import io
        import contextlib
        with FakeServer("good") as s:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = probe.main(["--endpoint", s.endpoint, "--markdown"], environ={})
            md = buf.getvalue()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                probe.main(["--endpoint", s.endpoint], environ={probe.TOKEN_ENV: TOKEN})
            js = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(len([l for l in md.splitlines() if l.startswith("| ") and "---" not in l]) - 1, 12)
        self.assertEqual(len(js), 12)
        self.assertNotIn(TOKEN, json.dumps(js))


def row(failed=None, passed=False, untested=False):
    steps = {s: "pass" for s in probe.PASS_STEPS} if passed else {}
    if failed:
        steps = {s: "pass" for s in probe.LADDER if probe.LADDER.index(s) < probe.LADDER.index(failed)}
        steps[failed] = "fail"
    if untested:
        steps = {s: "UNTESTED" for s in probe.ALL_STEPS}
    return {"steps": steps, "failed_step": failed, "untested": untested}


class Verdict(unittest.TestCase):  # T10
    def test_each_outcome(self):
        P, F = row(passed=True), row
        self.assertEqual(probe.verdict([P] * 8 + [F("2")] * 4)[0], "PLATFORM_READY")
        self.assertEqual(probe.verdict([P] * 5 + [F("2")] * 7)[0], "FIX_PROTOCOL_FIRST")
        self.assertEqual(probe.verdict([P] * 6 + [F("1d")] * 3 + [F("2")] * 3)[0], "AUTH_PATH_PER_AGENT")
        self.assertEqual(probe.verdict([P] * 3 + [F("2")] * 2 + [row(untested=True)] * 7)[0], "NOT_A_FIT")
        self.assertEqual(probe.verdict([P] * 5 + [F("2")] * 3 + [F("3")] * 2 + [F("1d")] * 2)[0], "NO_RULE_MATCHED")

    def test_precedence_ties(self):
        P = row(passed=True)
        # 8 pass beats 7 same-step failures cannot both hold in 12; use 8 pass + 4 auth failures
        self.assertEqual(probe.verdict([P] * 8 + [row("1d")] * 4)[0], "PLATFORM_READY")
        # FIX_PROTOCOL_FIRST over AUTH_PATH_PER_AGENT: 7 fail at 1b (also auth-step)
        self.assertEqual(probe.verdict([row("1b")] * 7 + [row("1d")] * 5)[0], "FIX_PROTOCOL_FIRST")
        # AUTH_PATH_PER_AGENT over NOT_A_FIT
        self.assertEqual(probe.verdict([row("1d")] * 3 + [P] * 1)[0], "AUTH_PATH_PER_AGENT")

    def test_untested_and_not_run_do_not_pass(self):
        nr = {"steps": {s: ("pass" if s in ("1a", "1b") else "not run (no token)") for s in probe.ALL_STEPS},
              "failed_step": None, "untested": False}
        self.assertEqual(probe.verdict([nr] * 12)[0], "NOT_A_FIT")
        self.assertEqual(probe.verdict([row(untested=True)] * 12)[0], "NOT_A_FIT")

    def test_accepts_run_profile_rows(self):
        with FakeServer("good") as s:
            r = run(s, TOKEN)
        self.assertEqual(probe.verdict([r] * 8)[0], "PLATFORM_READY")

    def test_pure(self):
        rows = [row(passed=True)] * 5 + [row("2")] * 7
        before = copy.deepcopy(rows)
        self.assertEqual(probe.verdict(rows), probe.verdict(rows))
        self.assertEqual(rows, before)


class Redact(unittest.TestCase):  # T11
    def test_keys_bearer_token_nested_and_query(self):
        tok = "tok-999"
        obj = {"a": {"access_token": "A1", "refresh_token": "R1", "client_secret": "C1",
                     "code": "K1", "code_verifier": "V1", "keep": "ok"},
               "list": ["Authorization: Bearer B1", "has tok-999 inside",
                        "https://x/cb?code=Q1&state=s&code_verifier=Q2",
                        '{"access_token": "J1", "error_code": "E1"}']}
        out = json.dumps(probe.redact(obj, tok))
        for secret in ("A1", "R1", "C1", "K1", "V1", "B1", "tok-999", "Q1", "Q2", "J1"):
            self.assertNotIn(secret, out)
        self.assertIn("ok", out)
        self.assertIn("E1", out)
        self.assertIn("state=s", out)


class Profiles(unittest.TestCase):  # T12
    NAMES = ["Claude Code", "Codex", "Cursor", "Devin", "Droid", "GitHub Copilot", "OpenCode",
             "Pi", "Vibe", "Visual Studio Code", "Command Code", "Bionic"]

    def test_committed_profiles(self):
        agents = probe.load_profiles()["agents"]
        self.assertEqual([a["name"] for a in agents], self.NAMES)
        for a in agents:
            for f in probe.FIELD_ENUMS:
                self.assertTrue(a[f]["doc_url"])
            if a["auth"]["value"] == "unknown":
                self.assertTrue(a["untested_reason"])

    def test_malformed_raises(self):
        with self.assertRaises(probe.ProfileError):
            probe.load_profiles(os.path.join(ROOT, "fixtures", "malformed-agents.json"))

    def test_report(self):
        if not os.path.exists(REPORT):
            self.skipTest("report not yet written")
        text = open(REPORT, encoding="utf-8").read()
        self.assertIn("pending live run (#290)", text)
        rows = [l for l in text.splitlines() if l.startswith("| ") and any(n in l.split("|")[1] for n in self.NAMES)]
        self.assertEqual(len(rows), 12)
        for l in rows:
            cells = [c.strip() for c in l.split("|")[1:-1]]
            self.assertIsNone(re.search(r"\b(1d|2|3)\b[^|;]*\[OBSERVED\b", cells[4]))


if __name__ == "__main__":
    unittest.main()
