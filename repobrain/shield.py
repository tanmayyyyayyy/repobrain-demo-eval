"""Shield: policy enforcement over paths and content. Pure functions, no I/O."""
import re

from . import policy as P

SECRET_PATTERNS = [
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe-live-key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("generic-secret", re.compile(
        r"""(?ix)\b(?:api[_-]?key|secret|passwd|password|token|auth)\w*\s*[:=]\s*
            ['"][A-Za-z0-9/+_\-=]{16,}['"]""")),
]
INJECTION_PATTERNS = [
    re.compile(r"(?i)\bignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier) "
               r"(?:instructions|prompts?|rules)"),
    re.compile(r"(?i)\bdisregard (?:all |any |the )?(?:previous|prior|above|system) "
               r"(?:instructions|prompts?|rules)"),
    re.compile(r"(?i)\byou are now (?:a|an|in)\b"),
    re.compile(r"(?i)\b(?:system|developer) prompt\b.{0,40}\b(?:reveal|print|show|leak|ignore)"),
    re.compile(r"(?i)\b(?:exfiltrate|send|post|upload)\b.{0,40}\b(?:secrets?|tokens?|api[_ -]?keys?"
               r"|credentials?|env(?:ironment)? variables?)\b"),
    re.compile(r"(?i)\bdo not (?:tell|inform|alert|notify) (?:the )?(?:user|human|reviewer|developer)"),
    re.compile(r"(?i)\b(?:AI|assistant|agent|copilot|claude)\b.{0,30}\b(?:must|should)\b.{0,30}"
               r"\b(?:disable|bypass|skip|turn off)\b.{0,30}\b(?:checks?|review|tests?|security|policy)"),
]
HIDDEN_CHARS = re.compile("[\u202a-\u202e\u2066-\u2069\u200b-\u200d\u2060\ufeff]")
MAX_BYTES = 1_000_000


def mask(s):
    return s[:4] + "…" + "*" * 6 if len(s) > 8 else "*" * len(s)


def finding(sev, rule, path, msg, line=None, evidence=None):
    f = {"severity": sev, "rule": rule, "file": path, "message": msg}
    if line:
        f["line"] = line
    if evidence:
        f["evidence"] = evidence
    return f


def scan_path(path, status, ai, policy):
    out = []
    pat = P.match_path(path, policy["blocked"])
    if pat:
        enforced = ai or policy["blocked_for"] == "all"
        who = "AI-authored change" if ai else "change"
        out.append(finding("block" if enforced else "warn", "blocked-path", path,
                           f"{who} touches protected path (matches '{pat}')"
                           + ("" if enforced else " - human author, flagged only")))
    pat = P.match_path(path, policy["review_required"])
    if pat and not P.match_path(path, policy["blocked"]):
        out.append(finding("warn", "review-required", path,
                           f"'{pat}' changes need explicit human review (dependency / build surface)"))
    if status == "D" and re.search(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|[^/]*\.test\.[jt]sx?$)", path):
        out.append(finding("warn", "test-deleted", path, "test file deleted in this change"))
    return out


def scan_text(path, text, policy, only_lines=None):
    """Content rules. `only_lines`: iterable of line numbers to inspect (None = whole text)."""
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES or "\x00" in text[:4096]:
        return []
    only = set(only_lines) if only_lines is not None else None
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if only is not None and n not in only:
            continue
        for rule, rx in SECRET_PATTERNS:
            m = rx.search(line)
            if m:
                out.append(finding("block", "secret", path,
                                   f"possible hardcoded credential ({rule})", n, mask(m.group(0))))
                break
        for rx in INJECTION_PATTERNS:
            if rx.search(line):
                out.append(finding("block", "prompt-injection", path,
                                   "text that tries to instruct an AI agent (possible injection)", n,
                                   line.strip()[:80]))
                break
        if HIDDEN_CHARS.search(line):
            out.append(finding("block", "hidden-unicode", path,
                               "invisible / bidirectional control characters (Trojan Source style)", n))
    return out


def check_action(action, policy):
    """Playground / agent-hook entry point: one proposed action -> decision."""
    path = action.get("path", "")
    ai = action.get("ai")
    if ai is None:
        ai = P.is_ai(action.get("actor", ""), policy)
    fs = scan_path(path, action.get("status", "M"), ai, policy)
    fs += scan_text(path, action.get("content", ""), policy)
    blocked = any(f["severity"] == "block" for f in fs)
    return {"decision": "BLOCKED" if blocked else "ALLOWED", "ai": ai, "findings": fs}
