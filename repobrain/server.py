"""Local review console. Binds to 127.0.0.1, requires a per-run token on every /api call,
validates the Host header (DNS-rebinding) and only accepts JSON POSTs (CSRF)."""
import json
import os
import secrets
import shutil
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import audit as A
from . import demo, explainer, gitutil, llm, optimizer, report, review, shield
from . import policy as P
from .cli import TEST_RX

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "index.html")
LOCK = threading.RLock()


class Ctx:
    def __init__(self, root, base, home, is_demo):
        self.root, self.base, self.home, self.demo = root, base, home, is_demo
        self.log = A.AuditLog(home)
        self.last_proposal = None

    def policy(self):
        return P.load_for(self.root, self.base)


def strip(prop):
    if not prop:
        return prop
    p = dict(prop)
    p["files"] = [{k: v for k, v in f.items() if k not in ("before", "after")} for f in prop["files"]]
    return p


def make_handler(ctx, token, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        server_version = "RepoBrain"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self):
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, {"error": "bad host"}); return False
            if self.path.startswith("/api/") and not secrets.compare_digest(
                    self.headers.get("X-RepoBrain-Token", ""), token):
                self._send(401, {"error": "missing or wrong token - open the URL printed by `repobrain serve`"}); return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            u = urlparse(self.path)
            try:
                if u.path == "/":
                    with open(STATIC, "rb") as f:
                        return self._send(200, f.read(), "text/html")
                if u.path == "/api/state":
                    return self._send(200, state(ctx))
                if u.path == "/api/audit":
                    es = ctx.log.entries()
                    return self._send(200, {"entries": es, "chain": A.verify_entries(es)})
                if u.path == "/api/report.md":
                    with LOCK:
                        pol = ctx.policy()
                        rev = review.run(ctx.root, pol, ctx.base, ctx.home)
                        return self._send(200, report.compliance(rev, ctx.log.entries(), ctx.log.verify(), pol),
                                          "text/markdown")
                self._send(404, {"error": "not found"})
            except Exception as e:
                self._send(500, {"error": str(e)})

        def do_POST(self):
            if not self._guard():
                return
            if "application/json" not in self.headers.get("Content-Type", ""):
                return self._send(415, {"error": "JSON only"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                return self._send(413, {"error": "body too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                with LOCK:
                    self._send(200, route(ctx, urlparse(self.path).path, body))
            except (ValueError, gitutil.GitError, P.PolicyError, KeyError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

    return H


def state(ctx):
    pol = ctx.policy()
    try:
        yml = gitutil.show(ctx.root, ctx.base, "repobrain.yml") or open(os.path.join(ctx.root, "repobrain.yml")).read()
    except OSError:
        yml = ""
    return {"repo": {"path": ctx.root, "branch": gitutil.branch(ctx.root), "head": gitutil.head(ctx.root),
                     "base": ctx.base, "demo": ctx.demo, "scenarios": list(demo.SCENARIOS) if ctx.demo else []},
            "policy": {k: v for k, v in pol.items() if not k.startswith("_")}, "policy_source": pol.get("_source"),
            "policy_yaml": yml, "llm": {"available": llm.available(), "allowed": pol["allow_llm"]}}


def route(ctx, path, body):
    actor = str(body.get("actor", "")).strip()[:80]
    if path == "/api/review":
        pol = ctx.policy()
        rev = review.run(ctx.root, pol, ctx.base, ctx.home, body.get("pr_author"))
        review.log_review(ctx.log, rev)
        return rev
    if path == "/api/optimize":
        pol, rev = ctx.policy(), None
        rev = review.run(ctx.root, pol, ctx.base, ctx.home)
        files = [f["path"] for f in rev["files"] if f["status"] != "D" and f["path"].endswith(".py")
                 and not TEST_RX.search(f["path"]) and not f["path"].endswith("__init__.py")]
        prop = optimizer.propose(ctx.root, pol, files, ctx.home, use_llm=bool(body.get("ai")))
        ctx.log.append("optimize", "repobrain", prop["status"], f"{len(prop['files'])} file(s) proposed, id {prop['id']}",
                       head=prop["head"])
        ctx.last_proposal = prop["id"]
        return strip(prop)
    if path == "/api/approve":
        if not actor:
            raise ValueError("approver name is required (a human approves)")
        res = optimizer.apply(ctx.root, ctx.home, body["id"], actor)
        ctx.log.append("approve", actor, "APPLIED", f"{', '.join(res['applied'])} -> {str(res['commit'])[:8]}", head=res["commit"])
        return res
    if path == "/api/waive":
        if not actor:
            raise ValueError("waiver name is required")
        w = review.waive(ctx.home, gitutil.head(ctx.root), actor, str(body.get("reason", "")))
        ctx.log.append("waive", actor, "WAIVED", w["reason"], head=gitutil.head(ctx.root))
        return w
    if path == "/api/shield":
        res = shield.check_action({"actor": str(body.get("actor", "")), "path": str(body.get("path", "")),
                                   "content": str(body.get("content", ""))}, ctx.policy())
        ctx.log.append("shield-check", str(body.get("actor", "") or "unknown")[:60], res["decision"],
                       f"{body.get('path', '')}: " + (", ".join(f['rule'] for f in res['findings']) or "clean"))
        return res
    if path == "/api/explain":
        pol = ctx.policy()
        ask = llm.ask if body.get("ai") and pol["allow_llm"] and llm.available() else None
        res = explainer.explain(str(body.get("log", ""))[:200_000], ask)
        ctx.log.append("explain", "repobrain", "EXPLAINED", res["summary"])
        return res
    if path == "/api/audit/tamper":
        return A.simulate_tamper(ctx.log.entries())
    if path == "/api/scenario" and ctx.demo:
        demo.switch(ctx.root, body["name"])
        ctx.base = demo.SCENARIO_BASES.get(body["name"], "main")
        return {"branch": gitutil.branch(ctx.root), "base": ctx.base}
    if path == "/api/reset" and ctx.demo:
        demo.seed(ctx.root)
        shutil.rmtree(ctx.home, ignore_errors=True)
        ctx.log = A.AuditLog(ctx.home)
        return {"ok": True}
    raise KeyError("unknown endpoint")


def serve(a):
    root = os.path.abspath(a.repo)
    is_demo = a.demo
    if is_demo:
        base_dir = os.environ.get("REPOBRAIN_DEMO_DIR") or os.path.join(os.path.expanduser("~"), ".repobrain", "demo")
        root, home = os.path.join(base_dir, "repo"), os.path.join(base_dir, "home")
        demo.seed(root)
        shutil.rmtree(home, ignore_errors=True)
        base = "main"
    else:
        base = gitutil.resolve_base(root, a.base)
        home = os.path.abspath(a.home) if a.home else A.home_for(root)
    ctx = Ctx(root, base, home, is_demo)
    token = secrets.token_urlsafe(24)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(ctx, token, a.port))
    url = f"http://127.0.0.1:{a.port}/?t={token}"
    print(f"RepoBrain console  ({'demo repo' if is_demo else root})\n  {url}\n  Ctrl+C to stop", flush=True)
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
