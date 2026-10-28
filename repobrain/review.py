"""PR review pipeline: git diff -> actors -> Shield -> complexity gate -> decision (+ human waivers)."""
import json
import os
import re
import time

from . import gitutil, metrics, shield
from . import policy as P

TEST_RX = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")


def _waivers(home):
    try:
        with open(os.path.join(home, "waivers.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def waive(home, head, actor, reason):
    if len(reason.strip()) < 10:
        raise ValueError("a waiver needs a written reason (min 10 characters)")
    w = _waivers(home)
    w[head] = {"actor": actor, "reason": reason.strip(), "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "waivers.json"), "w", encoding="utf-8") as f:
        json.dump(w, f)
    return w[head]


def run(root, policy, base=None, home=None, extra_actor=None):
    base = gitutil.resolve_base(root, base)
    head = gitutil.head(root)
    changes = gitutil.changed_files(root, base)
    added = gitutil.added_lines(root, base)
    actors = gitutil.file_actors(root, base)
    files, findings, gate = [], [], []
    for status, path in changes:
        ids = set(actors.get(path, set()))
        if extra_actor:
            ids.add(extra_actor)
        ai = any(P.is_ai(i, policy) for i in ids)
        ff = shield.scan_path(path, status, ai, policy)
        entry = {"path": path, "status": status, "actors": sorted(ids), "ai": ai, "findings": ff,
                 "violations": [], "added": len(added.get(path, {}))}
        if status != "D":
            text = gitutil.show(root, "HEAD", path)
            ff += shield.scan_text(path, text, policy, only_lines=added.get(path, {}).keys())
            if path.endswith(".py") and "python" in policy["languages"] and not TEST_RX.search(path):
                try:
                    funcs = metrics.analyze(text)
                    entry["metrics"] = metrics.summarize(funcs, text.count("\n"))
                    entry["violations"] = metrics.violations(funcs, policy, set(added.get(path, {})))
                except SyntaxError as e:
                    ff.append(shield.finding("warn", "syntax-error", path, f"cannot parse: {e.msg}", e.lineno))
        findings += ff
        gate += [dict(v, file=path) for v in entry["violations"]]
        files.append(entry)
    ai_files = [f for f in files if f["ai"]]
    if ai_files and len(files) > policy["max_files_per_run"]:
        findings.append(shield.finding("block", "blast-radius", "(change set)",
                                       f"AI-authored change touches {len(files)} files "
                                       f"(policy max_files_per_run = {policy['max_files_per_run']})"))
    if gitutil.git(root, "status", "--porcelain", check=False).strip():
        findings.append(shield.finding("warn", "dirty-tree", "(working tree)",
                                       "uncommitted changes present - review covers committed changes only"))
    blocks = [f for f in findings if f["severity"] == "block"]
    waiver = _waivers(home).get(head) if home else None
    if blocks:
        decision, why = "BLOCKED", f"{len(blocks)} blocking finding(s) - not waivable, fix and push"
    elif gate and policy["optimize"] == "required":
        if waiver:
            decision, why = "ALLOWED", f"complexity gate waived by {waiver['actor']}: {waiver['reason']}"
        else:
            decision, why = "CHANGES_REQUIRED", f"{len(gate)} function(s) over complexity limits - run the optimizer or waive with a reason"
    else:
        decision, why = "ALLOWED", "no blocking findings"
    return {"base": base, "head": head, "branch": gitutil.branch(root), "decision": decision, "reason": why,
            "files": files, "findings": findings, "gate": gate, "waiver": waiver,
            "counts": {"files": len(files), "ai_files": len(ai_files), "blocks": len(blocks),
                       "warnings": sum(1 for f in findings if f["severity"] == "warn"), "gate": len(gate)},
            "policy_source": policy.get("_source", "")}


def log_review(log, rev, actor="repobrain"):
    """Write one 'ai-change' entry per AI-authored file + one 'review' summary. Idempotent per head+decision."""
    recent = log.entries()[-60:]
    if any(e.get("event") == "review" and e.get("head") == rev["head"] and e.get("decision") == rev["decision"]
           for e in recent):
        return False
    for f in rev["files"]:
        if not f["ai"]:
            continue
        blocked = any(x["severity"] == "block" for x in f["findings"])
        warned = any(x["severity"] == "warn" for x in f["findings"])
        log.append("ai-change", ", ".join(f["actors"])[:120], "BLOCKED" if blocked else ("FLAGGED" if warned else "ALLOWED"),
                   f"{f['status']} {f['path']}: " + ("; ".join(x["rule"] for x in f["findings"]) or "clean"),
                   head=rev["head"])
    log.append("review", actor, rev["decision"], rev["reason"], head=rev["head"])
    return True
