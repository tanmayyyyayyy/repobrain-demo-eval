"""Append-only, hash-chained audit log (JSONL) with verification, export and tamper detection."""
import hashlib
import json
import os
import threading
import time

GENESIS = "0" * 64
_lock = threading.Lock()


def home_for(root):
    env = os.environ.get("REPOBRAIN_HOME")
    if env:
        return os.path.abspath(env)
    h = hashlib.sha1(os.path.abspath(root).encode()).hexdigest()[:10]
    return os.path.join(os.path.expanduser("~"), ".repobrain", h)


def _hash(rec):
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


class AuditLog:
    def __init__(self, home):
        self.path = os.path.join(home, "audit.jsonl")
        os.makedirs(home, exist_ok=True)

    def entries(self):
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    try:
                        out.append(json.loads(ln))
                    except json.JSONDecodeError:
                        out.append({"_corrupt": ln[:80]})
        return out

    def append(self, event, actor, decision, detail="", **extra):
        with _lock:
            es = self.entries()
            prev = es[-1].get("hash", GENESIS) if es else GENESIS
            rec = {"seq": len(es) + 1, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event,
                   "actor": actor, "decision": decision, "detail": detail, "prev": prev, **extra}
            rec["hash"] = _hash(rec)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return rec

    def verify(self, expect_head=None):
        return verify_entries(self.entries(), expect_head)


def verify_entries(es, expect_head=None):
    prev = GENESIS
    for i, e in enumerate(es):
        if "_corrupt" in e:
            return {"ok": False, "count": len(es), "broken_at": i + 1, "reason": "unparseable line"}
        if e.get("prev") != prev:
            return {"ok": False, "count": len(es), "broken_at": e.get("seq", i + 1),
                    "reason": "chain link broken (entry removed, inserted or reordered)"}
        if _hash(e) != e.get("hash"):
            return {"ok": False, "count": len(es), "broken_at": e.get("seq", i + 1),
                    "reason": "entry content was modified after being written"}
        prev = e["hash"]
    if expect_head and prev != expect_head:
        return {"ok": False, "count": len(es), "broken_at": None, "head": prev,
                "reason": "head hash differs from the anchored value (log truncated or rewritten)"}
    return {"ok": True, "count": len(es), "head": prev, "broken_at": None, "reason": ""}


def simulate_tamper(es):
    """Demo helper: edit one entry in memory (never on disk) and re-verify."""
    if not es:
        return {"ok": True, "note": "log is empty"}
    t = [dict(e) for e in es]
    i = len(t) // 2
    t[i]["decision"] = "ALLOWED" if t[i].get("decision") != "ALLOWED" else "BLOCKED"
    res = verify_entries(t)
    res["tampered_seq"] = t[i].get("seq")
    return res
