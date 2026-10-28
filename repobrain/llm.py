"""Optional LLM tier (stdlib only). Used ONLY when policy.allow_llm is true AND the caller opts in,
never on files with Shield findings, and every result is test-gated like any other change."""
import json
import os
import urllib.error
import urllib.request

API = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-3-5-sonnet-20241022"


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def ask(prompt, system="", max_tokens=2000):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
    model = os.environ.get("REPOBRAIN_MODEL", DEFAULT_MODEL)
    body = {"model": model, "max_tokens": max_tokens,
            "system": system, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(API, json.dumps(body).encode(), {
        "content-type": "application/json", "anthropic-version": "2023-06-01",
        "x-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
            err_data = json.loads(err_body)
            err_msg = err_data.get("error", {}).get("message", err_body)
        except Exception:
            err_msg = err_body or str(e)
        if e.code == 401:
            raise RuntimeError(f"Invalid Anthropic API key: {err_msg}") from e
        elif e.code in (400, 404) and ("model" in err_msg.lower() or "not_found" in err_msg.lower()):
            raise RuntimeError(f"Invalid Anthropic model '{model}': {err_msg}") from e
        else:
            raise RuntimeError(f"Anthropic API error ({e.code}): {err_msg}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error contacting Anthropic API: {e.reason}") from e

    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
