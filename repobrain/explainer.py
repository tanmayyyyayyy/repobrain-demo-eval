"""Failure Explainer: logs -> plain-language cause + fix. Deterministic rules first, optional LLM second."""
import re

from .shield import SECRET_PATTERNS

RULES = [
    (r"ModuleNotFoundError: No module named ['\"]?([\w.\-]+)", "Missing Python package",
     "The code imports '{0}' but it is not installed in this environment.",
     "Add '{0}' to requirements.txt (or pyproject) and reinstall."),
    (r"ImportError: cannot import name ['\"]?(\w+)", "Wrong or missing import",
     "'{0}' does not exist in the module being imported (renamed, removed, or circular import).",
     "Check the module for '{0}', fix the name, or break the import cycle."),
    (r"Cannot find module ['\"]([^'\"]+)", "Missing Node module",
     "Node cannot resolve '{0}'.", "Run `npm install {0}` (or fix the relative path)."),
    (r"npm ERR! (?:code )?ERESOLVE", "npm dependency conflict",
     "Two packages need incompatible versions of the same dependency.",
     "Align versions, or as a last resort use `npm install --legacy-peer-deps`."),
    (r"AttributeError: 'NoneType' object has no attribute ['\"]?(\w+)", "Value was None",
     "Something returned None and the code then used '.{0}' on it.",
     "Find the call that returned None; add a None check or fix the missing return."),
    (r"KeyError: ['\"]?([^'\"\n]+)", "Missing dictionary key",
     "The code read key '{0}' that is not in the dict.", "Use .get('{0}') or validate the input first."),
    (r"NameError: name ['\"]?(\w+)['\"]? is not defined", "Undefined name",
     "'{0}' is used before it is defined or never imported.", "Define or import '{0}'; check for typos."),
    (r"(?:Indentation|Syntax)Error: ([^\n]+)", "Syntax problem",
     "Python could not parse the file: {0}.", "Fix the line shown above (brackets, colons, indentation)."),
    (r"TypeError: ([^\n]+)", "Wrong type or arguments", "A value had the wrong type: {0}.",
     "Check argument counts/types at the call site shown in the traceback."),
    (r"ZeroDivisionError", "Division by zero", "A denominator was 0.", "Guard the division or validate input."),
    (r"FileNotFoundError: .*?['\"]([^'\"]+)['\"]", "File not found",
     "'{0}' does not exist where the code looks for it.", "Check the path / working directory / that the file is committed."),
    (r"PermissionError", "Permission denied", "The process may not read/write that path.", "Fix file permissions or run as the right user."),
    (r"(?:ConnectionRefusedError|ECONNREFUSED|Connection refused)", "Service not reachable",
     "Nothing is listening at the address (service down, wrong host/port, not started in CI).",
     "Start the dependency (docker service / mock) or fix host and port."),
    (r"(?:Timeout|timed out|ETIMEDOUT)", "Timeout", "An operation took longer than its limit.",
     "Check network/service health; raise the timeout only after finding the slow step."),
    (r"(?:exit code 137|Killed|OOMKilled|out of memory)", "Out of memory", "The process was killed for using too much memory.",
     "Reduce memory use (batching) or give the job more memory."),
    (r"no space left on device", "Disk full", "The runner/container ran out of disk.", "Clean caches/artifacts or use a larger runner."),
    (r"CONFLICT \(content\)|<<<<<<< ", "Merge conflict", "Unresolved merge conflict markers are in the code.",
     "Resolve the conflict markers, then commit."),
    (r"AssertionError", "Test assertion failed", "A test's expected value did not match the actual value.",
     "Compare 'expected' vs 'actual' in the output; fix the code or the stale test (not both blindly)."),
]
LOC_PY = re.compile(r'File "([^"]+)", line (\d+)')
LOC_PYTEST = re.compile(r"^([\w./\\-]+\.py):(\d+): ", re.M)
LOC_JS = re.compile(r"at (?:.*? \()?([\w./\\-]+\.[jt]sx?):(\d+):\d+")
FAILED = re.compile(r"^FAILED ([\w./\\:\[\]-]+)(?: - (.*))?$", re.M)


def redact(text):
    for _, rx in SECRET_PATTERNS:
        text = rx.sub("[REDACTED]", text)
    return text


def explain(log, ask=None):
    matches = []
    for rx, title, cause, fix in RULES:
        m = re.search(rx, log)
        if m:
            g = m.groups() or ("",)
            matches.append({"title": title, "cause": cause.format(*g), "fix": fix.format(*g),
                            "evidence": m.group(0)[:160]})
    loc = None
    for rx in (LOC_PYTEST, LOC_PY, LOC_JS):
        found = rx.findall(log)
        if found:
            loc = {"file": found[-1][0], "line": int(found[-1][1])}
            break
    failed = [{"test": a, "message": (b or "")[:160]} for a, b in FAILED.findall(log)]
    res = {"matches": matches, "location": loc, "failed_tests": failed,
           "summary": matches[0]["title"] if matches else "No known failure pattern matched",
           "llm": None}
    if ask:
        try:
            res["llm"] = ask("Explain this CI failure in 3 short sentences for a developer, then give the one "
                             "most likely fix:\n\n" + redact(log)[-6000:], "Be concrete. No preamble.")
        except Exception as e:
            res["llm"] = f"(LLM unavailable: {e})"
    return res
