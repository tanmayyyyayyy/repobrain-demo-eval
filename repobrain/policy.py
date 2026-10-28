"""Policy loading and validation. The policy is read from the TRUSTED base ref
when possible, so an AI-authored PR cannot loosen its own rules."""
import fnmatch
import re
import subprocess

import yaml

DEFAULT = {
    "optimize": "required",          # required | advisory
    "approval": "human",
    "ai_changes": "log_all",
    "complexity_threshold": 10,      # cyclomatic, per touched function
    "max_depth": 3,                  # nesting depth
    "max_cognitive": 15,             # cognitive complexity
    "max_files_per_run": 15,
    "require_tests": True,           # optimizer output must pass tests to be applicable
    "test_command": None,            # default: python -m pytest -q -x
    "test_timeout": 180,
    "blocked": [".github/workflows/", "secrets/", "credentials/", ".env", ".env.*",
                "*.pem", "id_rsa", "repobrain.yml"],
    "blocked_for": "ai",             # ai | all  (humans get a warning when "ai")
    "review_required": ["requirements.txt", "package.json", "pyproject.toml",
                        "Dockerfile", "package-lock.json"],
    "ai_actors": ["copilot", "claude", "codex", "cursor", "devin", "gemini", "aider",
                  "openai", "chatgpt", "bot", "agent"],
    "allow_llm": False,              # optimizer may send function code to an LLM API
    "remove_unused_imports": True,
    "allow_comment_loss": False,
    "languages": ["python"],
}
_TYPES = {
    "optimize": str, "approval": str, "ai_changes": str, "complexity_threshold": int,
    "max_depth": int, "max_cognitive": int, "max_files_per_run": int, "require_tests": bool,
    "test_timeout": int, "blocked": list, "blocked_for": str, "review_required": list,
    "ai_actors": list, "allow_llm": bool, "remove_unused_imports": bool,
    "allow_comment_loss": bool, "languages": list,
}
_ENUMS = {"optimize": {"required", "advisory"}, "approval": {"human"},
          "blocked_for": {"ai", "all"}}


class PolicyError(ValueError):
    pass


def parse(text):
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise PolicyError(f"repobrain.yml is not valid YAML: {e}")
    if not isinstance(raw, dict):
        raise PolicyError("repobrain.yml must be a mapping of keys to values")
    pol = {**DEFAULT}
    warnings = []
    for k, v in raw.items():
        if k not in DEFAULT:
            warnings.append(f"unknown key ignored: {k}")
            continue
        if k == "test_command":
            if v is not None and not isinstance(v, (str, list)):
                raise PolicyError("test_command must be a string or list")
        elif not isinstance(v, _TYPES[k]) or (_TYPES[k] is int and isinstance(v, bool)):
            raise PolicyError(f"{k} must be of type {_TYPES[k].__name__}")
        if k in _ENUMS and v not in _ENUMS[k]:
            raise PolicyError(f"{k} must be one of {sorted(_ENUMS[k])}")
        if _TYPES.get(k) is list and not all(isinstance(x, str) for x in v):
            raise PolicyError(f"{k} must be a list of strings")
        pol[k] = v
    pol["_warnings"] = warnings
    return pol


def load(path):
    with open(path, encoding="utf-8") as f:
        return parse(f.read())


def load_for(root, base=None):
    """Prefer the policy file as it exists on the trusted base ref."""
    if base:
        r = subprocess.run(["git", "show", f"{base}:repobrain.yml"], cwd=root,
                           capture_output=True, text=True)
        if r.returncode == 0:
            pol = parse(r.stdout)
            pol["_source"] = f"{base}:repobrain.yml (trusted base)"
            return pol
    try:
        pol = load(f"{root}/repobrain.yml")
        pol["_source"] = "working tree repobrain.yml"
        return pol
    except FileNotFoundError:
        pol = {**DEFAULT, "_warnings": ["no repobrain.yml found - using defaults"]}
        pol["_source"] = "built-in defaults"
        return pol


def is_ai(text, policy):
    low = text.lower()
    toks = set(re.split(r"[^a-z0-9]+", low))
    for frag in policy["ai_actors"]:
        f = frag.lower()
        if (f.isalnum() and f in toks) or (not f.isalnum() and f in low):
            return True
    return False


def match_path(path, patterns):
    """Return the first pattern that matches, else None."""
    base = path.rsplit("/", 1)[-1]
    for pat in patterns:
        if pat.endswith("/"):
            if path.startswith(pat) or ("/" + pat) in ("/" + path):
                return pat
        elif path == pat or base == pat or fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(path, pat):
            return pat
    return None
