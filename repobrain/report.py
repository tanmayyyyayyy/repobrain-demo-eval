"""Markdown output: PR comment and compliance report."""
ICON = {"BLOCKED": "⛔", "CHANGES_REQUIRED": "🟠", "ALLOWED": "✅"}


def pr_comment(rev, prop=None):
    c = rev["counts"]
    L = [f"## {ICON.get(rev['decision'], '')} RepoBrain: {rev['decision'].replace('_', ' ')}",
         f"_{rev['reason']}_", "",
         f"`{rev['base']}` → `{rev['head'][:8]}` · {c['files']} files ({c['ai_files']} AI-authored) · "
         f"{c['blocks']} blocking · {c['warnings']} warnings · {c['gate']} complexity violations", ""]
    blocks = [f for f in rev["findings"] if f["severity"] == "block"]
    warns = [f for f in rev["findings"] if f["severity"] == "warn"]
    if blocks:
        L += ["### Blocking", "| File | Line | Rule | Detail |", "|---|---|---|---|"]
        L += [f"| `{f['file']}` | {f.get('line', '')} | {f['rule']} | {f['message']} |" for f in blocks]
        L.append("")
    if rev["gate"]:
        L += ["### Complexity gate", "| Function | cyclomatic | cognitive | depth | Why |", "|---|---|---|---|---|"]
        L += [f"| `{g['file']}::{g['name']}` | {g['cc']} | {g['cog']} | {g['depth']} | {'; '.join(g['why'])} |"
              for g in rev["gate"]]
        L.append("")
    if warns:
        L += ["### Warnings"] + [f"- `{f['file']}` ({f['rule']}): {f['message']}" for f in warns] + [""]
    if prop and prop.get("files"):
        L += [f"### Proposed cleanup · tests: **{prop['status']}**", "| File | max cognitive | max depth | lines |", "|---|---|---|---|"]
        for f in prop["files"]:
            b, a = f["metrics"]["before"], f["metrics"]["after"]
            L.append(f"| `{f['path']}` | {b['max_cog']} → {a['max_cog']} | {b['max_depth']} → {a['max_depth']} | {b['loc']} → {a['loc']} |")
        L.append("")
    L.append("<sub>Policy is read from the trusted base branch. Merge still requires human approval.</sub>")
    return "\n".join(L)


def compliance(rev, entries, chain, policy):
    L = ["# RepoBrain compliance report", "",
         f"- Branch `{rev['branch']}` @ `{rev['head'][:8]}` vs `{rev['base']}`",
         f"- Decision: **{rev['decision']}** - {rev['reason']}",
         f"- Policy source: {rev.get('policy_source', '')}",
         f"- Audit chain: **{'VERIFIED' if chain['ok'] else 'BROKEN'}** ({chain['count']} entries, head `{str(chain.get('head', ''))[:16]}`)", "",
         "## Policy in force", "```"]
    L += [f"{k}: {v}" for k, v in policy.items() if not k.startswith("_")]
    L += ["```", "", "## Files", "| File | Status | AI-authored | Actors |", "|---|---|---|---|"]
    L += [f"| `{f['path']}` | {f['status']} | {'yes' if f['ai'] else 'no'} | {', '.join(f['actors'])} |" for f in rev["files"]]
    L += ["", "## Audit trail", "| # | Time | Event | Actor | Decision | Detail |", "|---|---|---|---|---|---|"]
    L += [f"| {e.get('seq')} | {e.get('ts')} | {e.get('event')} | {e.get('actor')} | {e.get('decision')} | {str(e.get('detail'))[:90]} |"
          for e in entries]
    return "\n".join(L)
