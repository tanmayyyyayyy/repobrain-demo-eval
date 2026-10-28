"""RepoBrain CLI.  python -m repobrain <command>"""
import argparse
import json
import os
import sys

from . import audit as A
from . import explainer, gitutil, llm, metrics, optimizer, report, review, shield
from . import policy as P

TEST_RX = review.TEST_RX


def _ctx(a):
    root = os.path.abspath(a.repo)
    base = None
    if gitutil.is_repo(root):
        try:
            base = gitutil.resolve_base(root, getattr(a, "base", None))
        except gitutil.GitError:
            base = None
    pol = P.load_for(root, base)
    home = os.path.abspath(a.home) if getattr(a, "home", None) else A.home_for(root)
    return root, base, pol, home, A.AuditLog(home)


def _py_files(root):
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in optimizer.SKIP_DIRS]
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")
            if f.endswith(".py") and not TEST_RX.search(rel) and f != "__init__.py" and f != "conftest.py":
                out.append(rel)
    return sorted(out)


def cmd_init(a):
    dst = os.path.join(a.repo, "repobrain.yml")
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "repobrain.yml")
    if os.path.exists(dst):
        print("repobrain.yml already exists"); return 1
    with open(src, encoding="utf-8") as f, open(dst, "w", encoding="utf-8") as g:
        g.write(f.read())
    print("wrote repobrain.yml - commit it to your default branch so PRs cannot change it")
    return 0


def cmd_review(a):
    root, base, pol, home, log = _ctx(a)
    rev = review.run(root, pol, base, home, a.actor)
    review.log_review(log, rev)
    md = report.pr_comment(rev)
    if a.comment:
        with open(a.comment, "w", encoding="utf-8") as f:
            f.write(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md + "\n")
    print(json.dumps(rev, indent=2) if a.json else md)
    return 0 if rev["decision"] == "ALLOWED" else 1


def cmd_scan(a):
    root, _, pol, _, _ = _ctx(a)
    rows = []
    for rel in _py_files(root):
        try:
            src = open(os.path.join(root, rel), encoding="utf-8").read()
            rows += [dict(f, file=rel) for f in metrics.violations(metrics.analyze(src), pol)]
        except (SyntaxError, UnicodeDecodeError):
            pass
    rows.sort(key=lambda r: -r["cog"])
    print(f"{len(rows)} function(s) over policy limits")
    for r in rows:
        print(f"  {r['file']}::{r['name']:<28} cc={r['cc']:<3} cog={r['cog']:<3} depth={r['depth']}  ({'; '.join(r['why'])})")
    return 0


def cmd_shield(a):
    root, base, pol, _, log = _ctx(a)
    content = open(a.file, encoding="utf-8").read() if a.file else ""
    res = shield.check_action({"actor": a.actor, "path": a.path, "content": content}, pol)
    log.append("shield-check", a.actor, res["decision"], f"{a.path}: " + (", ".join(f["rule"] for f in res["findings"]) or "clean"))
    print(json.dumps(res, indent=2))
    return 0 if res["decision"] == "ALLOWED" else 1


def cmd_optimize(a):
    root, base, pol, home, log = _ctx(a)
    if a.all or not base:
        files = _py_files(root)
    else:
        files = [p for s, p in gitutil.changed_files(root, base)
                 if s != "D" and p.endswith(".py") and not TEST_RX.search(p) and not p.endswith("__init__.py")]
    prop = optimizer.propose(root, pol, files, home, use_llm=a.ai)
    log.append("optimize", "repobrain", prop["status"], f"{len(prop['files'])} file(s) proposed, id {prop['id']}")
    print(f"status: {prop['status']}   tests: {prop['tests'].get('ok')}  ({prop['tests'].get('cmd', '')})")
    if prop["llm"]["requested"]:
        print(f"llm: used={prop['llm']['used']} {prop['llm']['reason']}")
    for f in prop["files"]:
        b, af = f["metrics"]["before"], f["metrics"]["after"]
        print(f"  {f['path']}: cognitive {b['max_cog']}->{af['max_cog']}  depth {b['max_depth']}->{af['max_depth']}  "
              f"lines {b['loc']}->{af['loc']}  rules={f['notes']}")
        if a.diff:
            print(f["diff"])
    for s in prop["skipped"]:
        print(f"  skipped {s.get('file', '')}::{s['function']}: {s['reason']}")
    if a.apply:
        if not a.actor:
            print("--apply needs --actor <your name> (a human approves)"); return 2
        res = optimizer.apply(root, home, prop["id"], a.actor)
        log.append("approve", a.actor, "APPLIED", f"{', '.join(res['applied'])} -> {res['commit']}", head=res["commit"])
        print("applied:", res)
    return 0 if prop["status"] in ("verified", "nothing-to-optimize") else 1


def cmd_explain(a):
    log_text = open(a.log, encoding="utf-8", errors="replace").read() if a.log else sys.stdin.read()
    ask = None
    _, _, pol, _, _ = _ctx(a)
    if a.ai and pol["allow_llm"] and llm.available():
        ask = llm.ask
    res = explainer.explain(log_text, ask)
    print(json.dumps(res, indent=2))
    return 0


def cmd_audit(a):
    _, _, _, _, log = _ctx(a)
    v = log.verify(a.expect)
    if a.export:
        print(json.dumps({"verification": v, "entries": log.entries()}, indent=2))
    else:
        for e in log.entries()[-a.tail:]:
            print(f"#{e['seq']:<4} {e['ts']}  {e['event']:<10} {e['decision']:<17} {e['actor'][:30]:<30} {e['detail'][:70]}")
        print("chain:", "VERIFIED" if v["ok"] else f"BROKEN at #{v['broken_at']} - {v['reason']}", f"head={str(v.get('head'))[:16]}")
    return 0 if v["ok"] else 1


def cmd_waive(a):
    root, base, pol, home, log = _ctx(a)
    w = review.waive(home, gitutil.head(root), a.actor, a.reason)
    log.append("waive", a.actor, "WAIVED", w["reason"], head=gitutil.head(root))
    print("waived:", w)
    return 0


def cmd_report(a):
    root, base, pol, home, log = _ctx(a)
    rev = review.run(root, pol, base, home)
    md = report.compliance(rev, log.entries(), log.verify(), {k: v for k, v in pol.items()})
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(md)
        print("wrote", a.out)
    else:
        print(md)
    return 0


def cmd_serve(a):
    from .server import serve
    return serve(a)


def build():
    ap = argparse.ArgumentParser(prog="repobrain", description=__doc__)
    ap.add_argument("--repo", default=".", help="repository path (default: .)")
    ap.add_argument("--home", help="where audit log/proposals live (default ~/.repobrain/<repo-hash>)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, help="write a starter repobrain.yml")
    sp = add("review", cmd_review, help="Shield + complexity gate on the PR diff (CI entry point)")
    sp.add_argument("--base"); sp.add_argument("--actor", help="extra identity to treat as the author (e.g. PR opener)")
    sp.add_argument("--comment", help="write PR-comment markdown here"); sp.add_argument("--json", action="store_true")
    add("scan", cmd_scan, help="list over-limit functions across the whole repo (tech debt)")
    sp = add("shield", cmd_shield, help="check one proposed action"); sp.add_argument("--path", required=True)
    sp.add_argument("--actor", default="unknown"); sp.add_argument("--file")
    sp = add("optimize", cmd_optimize, help="propose a test-verified cleanup")
    sp.add_argument("--base"); sp.add_argument("--all", action="store_true"); sp.add_argument("--ai", action="store_true")
    sp.add_argument("--diff", action="store_true"); sp.add_argument("--apply", action="store_true")
    sp.add_argument("--actor", help="approving human (required with --apply)")
    sp = add("explain", cmd_explain, help="explain a failing log (stdin or --log)")
    sp.add_argument("--log"); sp.add_argument("--ai", action="store_true")
    sp = add("audit", cmd_audit, help="show / verify / export the audit log")
    sp.add_argument("--expect", help="anchored head hash to verify against"); sp.add_argument("--export", action="store_true")
    sp.add_argument("--tail", type=int, default=25)
    sp = add("waive", cmd_waive, help="human override of the complexity gate (logged)")
    sp.add_argument("--actor", required=True); sp.add_argument("--reason", required=True)
    sp = add("report", cmd_report, help="compliance report (markdown)"); sp.add_argument("--base"); sp.add_argument("--out")
    sp = add("serve", cmd_serve, help="review console (localhost)")
    sp.add_argument("--base"); sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--demo", action="store_true", help="seed a demo git repo with AI-authored PRs")
    sp.add_argument("--no-browser", action="store_true")
    return ap


def main(argv=None):
    a = build().parse_args(argv)
    try:
        return a.fn(a)
    except (gitutil.GitError, P.PolicyError, ValueError) as e:
        print(f"repobrain: {e}", file=sys.stderr)
        return 2
