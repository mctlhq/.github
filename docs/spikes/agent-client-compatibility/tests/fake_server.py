"""Loopback fake of api.mctl.ai's MCP/OAuth surface with selectable failure variants."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "fixtures")
TOKEN = "test-token-abc123"
VARIANTS = ("good", "wrong_path", "bad_bearer", "no_dcr", "empty_skills", "no_prompt")


def _fixture(name, base):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.loads(fh.read().replace("{base}", base))


class FakeServer:
    def __init__(self, variant="good"):
        self.variant = variant
        self.requests = []  # (method, path, body)
        self.user_agents = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj=None, headers=None, raw=None, ctype="application/json"):
                body = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode() if n else ""
                try:
                    return json.loads(raw) if raw else None
                except ValueError:
                    return raw

            def do_GET(self):
                outer.requests.append(("GET", self.path, None))
                outer.user_agents.append(self.headers.get("User-Agent"))
                base = outer.base
                if self.path == "/.well-known/oauth-protected-resource/mcp":
                    return self._send(200, _fixture("prm.json", base))
                if self.path == "/.well-known/oauth-authorization-server":
                    meta = _fixture("as-metadata.json", base)
                    if outer.variant == "no_dcr":
                        del meta["registration_endpoint"]
                    return self._send(200, meta)
                self._send(404, {"error": "not_found"})

            def do_POST(self):
                body = self._body()
                outer.requests.append(("POST", self.path, body))
                outer.user_agents.append(self.headers.get("User-Agent"))
                if self.path == "/oauth/register":
                    return self._send(201, {"client_id": "client-xyz", "client_secret": "s3cret-value"})
                if self.path != "/mcp" or outer.variant == "wrong_path":
                    return self._send(404, {"error": "not_found"})
                auth = self.headers.get("Authorization", "")
                challenge = 'Bearer realm="%s", resource_metadata="%s/.well-known/oauth-protected-resource/mcp"' % (
                    outer.base, outer.base)
                if not auth:
                    return self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": challenge})
                if outer.variant == "bad_bearer" or auth != "Bearer " + TOKEN:
                    return self._send(401, {"error": "invalid_token"},
                                      {"WWW-Authenticate": 'Bearer error="invalid_token", ' + challenge[7:]})
                if not isinstance(body, dict) or "id" not in body:
                    return self._send(202, raw=b"")
                m = body["method"]
                if m == "initialize":
                    res = {"protocolVersion": "2025-06-18", "capabilities": {},
                           "serverInfo": {"name": "fake-mctl", "version": "0"}}
                elif m == "tools/call":
                    name = body["params"]["name"]
                    if name == "mctl_list_platform_skills":
                        skills = [] if outer.variant == "empty_skills" else [{"name": "deploy"}]
                        res = {"content": [{"type": "text", "text": json.dumps({"skills": skills})}]}
                    else:
                        res = {"content": [{"type": "text", "text": "# deploy skill body"}]}
                elif m == "prompts/list":
                    ps = [] if outer.variant == "no_prompt" else [{"name": "platform-skill"}]
                    res = {"prompts": ps}
                elif m == "prompts/get":
                    res = {"messages": [{"role": "user", "content": {"type": "text", "text": "x"}}]}
                else:
                    return self._send(200, {"jsonrpc": "2.0", "id": body["id"],
                                            "error": {"code": -32601, "message": "no"}})
                msg = {"jsonrpc": "2.0", "id": body["id"], "result": res}
                if m == "tools/call":  # exercise the event-stream path
                    return self._send(200, raw=("event: message\ndata: %s\n\n" % json.dumps(msg)).encode(),
                                      ctype="text/event-stream")
                self._send(200, msg)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.endpoint = self.base + "/mcp"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def paths(self, method=None):
        return [p for m, p, _ in self.requests if method in (None, m)]
