"""Code metrics: cyclomatic (radon-style), cognitive (Sonar-style, approximated), nesting depth."""
import ast

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = FUNC + (ast.Lambda, ast.ClassDef)
LOOPS = (ast.For, ast.AsyncFor, ast.While)
COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)
TRY = tuple(t for t in (getattr(ast, "Try", None), getattr(ast, "TryStar", None)) if t)


def _own(fn):
    """Nodes inside fn, not descending into nested scopes."""
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(n))


def cyclomatic(fn):
    c = 1
    for n in _own(fn):
        if isinstance(n, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
                          ast.With, ast.AsyncWith, ast.Assert, ast.match_case)):
            c += 1
        elif isinstance(n, ast.BoolOp):
            c += len(n.values) - 1
        elif isinstance(n, ast.comprehension):
            c += 1 + len(n.ifs)
    return c


def _blocks(st):
    if isinstance(st, ast.If):
        return [st.body, st.orelse]
    if isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
        return [st.body, st.orelse]
    if isinstance(st, (ast.With, ast.AsyncWith)):
        return [st.body]
    if isinstance(st, TRY):
        return [st.body] + [h.body for h in st.handlers] + [st.orelse, st.finalbody]
    if isinstance(st, ast.Match):
        return [c.body for c in st.cases]
    return []


def depth(stmts, d=0):
    m = d
    for st in stmts:
        if isinstance(st, SCOPES):
            continue
        if isinstance(st, ast.If):
            m = max(m, depth(st.body, d + 1))
            if len(st.orelse) == 1 and isinstance(st.orelse[0], ast.If):
                m = max(m, depth(st.orelse, d))          # elif stays on the same level
            else:
                m = max(m, depth(st.orelse, d + 1))
        elif isinstance(st, COMPOUND):
            for b in _blocks(st):
                m = max(m, depth(b, d + 1))
    return m


def _bool_ops(node):
    return sum(1 for n in ast.walk(node) if isinstance(n, (ast.BoolOp, ast.IfExp)))


def cognitive(fn):
    def block(stmts, nest):
        return sum(stmt(s, nest, False) for s in stmts)

    def stmt(st, nest, is_elif):
        if isinstance(st, SCOPES):
            return block(st.body, nest + 1) if isinstance(st, FUNC) else 0
        if isinstance(st, ast.If):
            s = (1 if is_elif else 1 + nest) + _bool_ops(st.test) + block(st.body, nest + 1)
            if len(st.orelse) == 1 and isinstance(st.orelse[0], ast.If):
                s += stmt(st.orelse[0], nest, True)
            elif st.orelse:
                s += 1 + block(st.orelse, nest + 1)
            return s
        if isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
            head = st.test if isinstance(st, ast.While) else st.iter
            return 1 + nest + _bool_ops(head) + block(st.body, nest + 1) + block(st.orelse, nest + 1)
        if isinstance(st, TRY):
            s = block(st.body, nest) + block(st.orelse, nest) + block(st.finalbody, nest)
            for h in st.handlers:
                s += 1 + nest + block(h.body, nest + 1)
            return s
        if isinstance(st, (ast.With, ast.AsyncWith)):
            return block(st.body, nest)
        if isinstance(st, ast.Match):
            return 1 + nest + sum(block(c.body, nest + 1) for c in st.cases)
        return _bool_ops(st)

    return block(fn.body, 0)


def measure(fn):
    return {"cc": cyclomatic(fn), "cog": cognitive(fn), "depth": depth(fn.body),
            "lines": (fn.end_lineno - fn.lineno + 1)}


def analyze(src):
    """-> list of per-function dicts (qualified names, line spans, metrics)."""
    tree = ast.parse(src)
    out = []

    def visit(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, FUNC):
                start = min([d.lineno for d in ch.decorator_list] + [ch.lineno])
                out.append({"name": prefix + ch.name, "line": ch.lineno, "start": start,
                            "end": ch.end_lineno, **measure(ch)})
                visit(ch, prefix + ch.name + ".")
            elif isinstance(ch, ast.ClassDef):
                visit(ch, prefix + ch.name + ".")
            else:
                visit(ch, prefix)

    visit(tree, "")
    return out


def summarize(funcs, loc=0):
    return {"functions": len(funcs),
            "max_cc": max([f["cc"] for f in funcs], default=0),
            "max_cog": max([f["cog"] for f in funcs], default=0),
            "max_depth": max([f["depth"] for f in funcs], default=0),
            "total_cog": sum(f["cog"] for f in funcs), "loc": loc}


def violations(funcs, policy, touched=None):
    """Functions over policy thresholds. `touched`: set of changed line numbers (None = all)."""
    out = []
    for f in funcs:
        if touched is not None and not any(f["start"] <= ln <= f["end"] for ln in touched):
            continue
        why = []
        if f["cc"] > policy["complexity_threshold"]:
            why.append(f"cyclomatic {f['cc']} > {policy['complexity_threshold']}")
        if f["depth"] > policy["max_depth"]:
            why.append(f"nesting depth {f['depth']} > {policy['max_depth']}")
        if f["cog"] > policy["max_cognitive"]:
            why.append(f"cognitive {f['cog']} > {policy['max_cognitive']}")
        if why:
            out.append({**f, "why": why})
    return out
