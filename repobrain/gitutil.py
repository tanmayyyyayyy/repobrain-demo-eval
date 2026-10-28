"""Thin git helpers (subprocess, no dependencies)."""
import re
import subprocess

from . import policy as P


class GitError(RuntimeError):
    pass


def git(root, *args, check=True, input=None):
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                       input=input, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise GitError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout


def is_repo(root):
    return subprocess.run(["git", "rev-parse", "--git-dir"], cwd=root,
                          capture_output=True).returncode == 0


def head(root):
    return git(root, "rev-parse", "HEAD").strip()


def branch(root):
    return git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()


def resolve_base(root, base=None):
    for cand in [base, "origin/main", "main", "origin/master", "master"]:
        if cand and subprocess.run(["git", "rev-parse", "--verify", "-q", cand], cwd=root,
                                   capture_output=True).returncode == 0:
            return cand
    raise GitError("could not find a base ref (try --base main)")


def changed_files(root, base):
    out = git(root, "diff", "--name-status", "--no-renames", f"{base}...HEAD")
    res = []
    for ln in out.splitlines():
        st, _, path = ln.partition("\t")
        if path:
            res.append((st[0], path))
    return res


def added_lines(root, base):
    """{path: {line_no: text}} for lines added in base...HEAD."""
    out = git(root, "diff", "-U0", "--no-renames", f"{base}...HEAD")
    res, path, ln = {}, None, 0
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@") and path:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            ln = int(m.group(1))
            res.setdefault(path, {})
        elif path and line.startswith("+") and not line.startswith("+++"):
            res[path][ln] = line[1:]
            ln += 1
    return res


def file_actors(root, base):
    """{path: set(identity strings)} from commit authors/committers/co-author trailers."""
    out = git(root, "log", f"{base}..HEAD", "--name-only",
              "--format=%x1e%H%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1f")
    actors = {}
    for rec in out.split("\x1e")[1:]:
        parts = rec.split("\x1f")
        if len(parts) < 5:
            continue
        _, author, committer, body, tail = parts[:5]
        ids = {author, committer}
        ids.update(m.strip() for m in re.findall(r"(?im)^co-authored-by:\s*(.+)$", body))
        for p in tail.split("\n"):
            if p.strip():
                actors.setdefault(p.strip(), set()).update(ids)
    return actors


def show(root, ref, path):
    return git(root, "show", f"{ref}:{path}", check=False)
