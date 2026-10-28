"""Behaviour-preserving AST rewrites + source splicing that leaves untouched code byte-identical.

Rules (each is semantics-preserving):
  else-after-exit   `if c: return x  else: B`            -> `if c: return x` ; B
  guard-clause      trailing `if c: BODY` in a function  -> `if not c: return` ; BODY
                    trailing `if c: BODY` in a loop      -> `if not c: continue` ; BODY
  merge-ifs         `if a: if b: X` (no else)            -> `if a and b: X`
  bool-return       `if c: return True` / `return False` -> `return bool(c)`
  loop-to-comp      `r=[]; for x in xs: if c: r.append(f(x))` -> `r = [f(x) for x in xs if c]`
  is-none           `x == None`                          -> `x is None`
"""
import ast
import copy
import io
import re
import tokenize

EXIT = (ast.Return, ast.Raise, ast.Continue, ast.Break)
MARK = re.compile(r"^(\s*)__rbc__\((\d+)\)\s*$")
BAD_IN_COMP = (ast.Yield, ast.YieldFrom, ast.Await, ast.NamedExpr)


# ───────────────────────── comment preservation ─────────────────────────
def mark_comments(src):
    """Turn standalone comment lines into `__rbc__(n)` statements (same line count) so they
    survive ast.unparse. Returns (marked_src, {n: comment_text})."""
    lines = src.splitlines()
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src, {}
    depth, cand = 0, []
    for i, t in enumerate(toks):
        if t.type == tokenize.OP and t.string in "([{":
            depth += 1
        elif t.type == tokenize.OP and t.string in ")]}":
            depth -= 1
        elif t.type == tokenize.COMMENT and depth == 0 and lines[t.start[0] - 1].lstrip().startswith("#"):
            nxt = next((x for x in toks[i + 1:] if x.type not in (
                tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)), None)
            if nxt and nxt.type != tokenize.ENDMARKER and nxt.string not in (
                    "else", "elif", "except", "finally"):
                cand.append((t.start[0], t.string, nxt.start[1]))
    comments, out = {}, list(lines)
    for n, (ln, text, indent) in enumerate(cand):
        comments[n] = text
        out[ln - 1] = " " * indent + f"__rbc__({n})"
    return "\n".join(out) + ("\n" if src.endswith("\n") else ""), comments


def restore_comments(text, comments):
    res = []
    for ln in text.split("\n"):
        m = MARK.match(ln)
        res.append(m.group(1) + comments[int(m.group(2))] if m and int(m.group(2)) in comments else ln)
    return "\n".join(res)


def count_comments(text):
    try:
        return sum(1 for t in tokenize.generate_tokens(io.StringIO(text).readline)
                   if t.type == tokenize.COMMENT)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text.count("#")


def requote(text, prefer_double):
    """ast.unparse emits 'single' quotes; restore the file's dominant style on simple literals."""
    if not prefer_double:
        return text
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text
    lines = text.split("\n")
    for t in reversed(toks):
        if t.type == tokenize.STRING and t.start[0] == t.end[0]:
            m = re.match(r"^([rbuRBU]{0,2})'([^'\"\\\n]*)'$", t.string)
            if m:
                row, (c0, c1) = t.start[0] - 1, (t.start[1], t.end[1])
                lines[row] = lines[row][:c0] + f'{m.group(1)}"{m.group(2)}"' + lines[row][c1:]
    return "\n".join(lines)


# ───────────────────────────── AST rules ─────────────────────────────
def negate(test):
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return test.operand
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        flip = {ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In,
                ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}.get(type(test.ops[0]))
        if flip:
            return ast.Compare(left=test.left, ops=[flip()], comparators=test.comparators)
    return ast.UnaryOp(op=ast.Not(), operand=test)


def _is_bool(n, val):
    return isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and n.value.value is val


def _always_bool(test):
    return isinstance(test, ast.Compare) or (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not))


def terminates(stmts):
    """True if control can never fall off the end of this statement list."""
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, EXIT):
        return True
    return isinstance(last, ast.If) and bool(last.orelse) and terminates(last.body) and terminates(last.orelse)


def _weight(stmts):
    return sum(1 for s in stmts for _ in ast.walk(s))


def _short_exit(st):
    flat = not any(isinstance(n, ast.stmt) and isinstance(n, (ast.If, ast.For, ast.While, ast.Try, ast.With))
                   for s in st.orelse for n in ast.walk(s))
    return flat and len(st.orelse) <= 2 and _weight(st.orelse) < _weight(st.body)


def _worth_guard(body):
    return len(body) >= 2 or isinstance(body[0], (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With))


class Rewriter(ast.NodeTransformer):
    def __init__(self):
        self.notes = {}
        self.fn_stack = []

    def note(self, rule):
        self.notes[rule] = self.notes.get(rule, 0) + 1

    # -- expression rule
    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.comparators[0], ast.Constant) \
                and node.comparators[0].value is None and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            node.ops = [ast.Is() if isinstance(node.ops[0], ast.Eq) else ast.IsNot()]
            self.note("is-none")
        return node

    # -- scopes
    def visit_FunctionDef(self, node):
        self.fn_stack.append(node)
        self.generic_visit(node)
        self.fn_stack.pop()
        node.body = self._guard(node.body, ast.Return())
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_For(self, node):
        self.generic_visit(node)
        node.body = self._guard(node.body, ast.Continue())
        return node

    visit_While = visit_For
    visit_AsyncFor = visit_For

    def generic_visit(self, node):
        super().generic_visit(node)
        for field in ("body", "orelse", "finalbody"):
            v = getattr(node, field, None)
            if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
                setattr(node, field, self.block(v))
        return node

    # -- statement-list rules
    def _guard(self, body, exit_stmt):
        while body and isinstance(body[-1], ast.If) and not body[-1].orelse and _worth_guard(body[-1].body):
            last = body[-1]
            body = body[:-1] + [ast.If(test=negate(last.test), body=[copy.deepcopy(exit_stmt)], orelse=[])] + last.body
            self.note("guard-clause")
        return body

    def block(self, stmts):
        changed = True
        while changed:
            changed, out = False, []
            for st in stmts:
                if isinstance(st, ast.If) and st.orelse and terminates(st.body):
                    if terminates(st.orelse) and _short_exit(st):
                        # both branches leave: hoist the short one as a guard, un-nest the long one
                        out.append(ast.If(test=negate(st.test), body=st.orelse, orelse=[]))
                        out.extend(st.body)
                        self.note("guard-clause")
                    else:
                        out.append(ast.If(test=st.test, body=st.body, orelse=[]))
                        out.extend(st.orelse)
                        self.note("else-after-exit")
                    changed = True
                else:
                    out.append(st)
            stmts = out
        out, i = [], 0
        while i < len(stmts):
            st = stmts[i]
            nxt = stmts[i + 1] if i + 1 < len(stmts) else None
            if isinstance(st, ast.If) and not st.orelse and len(st.body) == 1 and isinstance(st.body[0], ast.If) \
                    and not st.body[0].orelse:
                inner = st.body[0]
                out.append(ast.If(test=ast.BoolOp(op=ast.And(), values=[st.test, inner.test]),
                                  body=inner.body, orelse=[]))
                self.note("merge-ifs")
            elif isinstance(st, ast.If) and not st.orelse and len(st.body) == 1 and nxt is not None:
                if _is_bool(st.body[0], True) and _is_bool(nxt, False):
                    out.append(ast.Return(value=st.test if _always_bool(st.test) else
                                          ast.Call(func=ast.Name("bool", ast.Load()), args=[st.test], keywords=[])))
                    self.note("bool-return"); i += 1
                elif _is_bool(st.body[0], False) and _is_bool(nxt, True):
                    out.append(ast.Return(value=negate(st.test)))
                    self.note("bool-return"); i += 1
                else:
                    out.append(st)
            elif self._append_loop(st, out):
                pass
            else:
                out.append(st)
            i += 1
        if len(out) != len(stmts) or any(a is not b for a, b in zip(out, stmts)):
            if any(n in self.notes for n in ("merge-ifs", "bool-return")) and self._needs_repeat(out):
                return self.block(out)
        return out

    @staticmethod
    def _needs_repeat(out):
        return any(isinstance(s, ast.If) and not s.orelse and len(s.body) == 1 and isinstance(s.body[0], ast.If)
                   and not s.body[0].orelse for s in out)

    def _append_loop(self, st, out):
        """`r = []` + `for x in xs: [if c:] r.append(e)` -> comprehension."""
        if not (isinstance(st, ast.For) and not st.orelse and out and len(st.body) == 1):
            return False
        init = out[-1]
        if not (isinstance(init, ast.Assign) and len(init.targets) == 1 and isinstance(init.targets[0], ast.Name)
                and isinstance(init.value, ast.List) and not init.value.elts):
            return False
        acc, body, conds = init.targets[0].id, st.body[0], []
        if isinstance(body, ast.If) and not body.orelse and len(body.body) == 1:
            conds, body = [body.test], body.body[0]
        call = body.value if isinstance(body, ast.Expr) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "append"
                and isinstance(call.func.value, ast.Name) and call.func.value.id == acc
                and len(call.args) == 1 and not call.keywords):
            return False
        parts = [st.iter, call.args[0]] + conds
        if any(isinstance(n, BAD_IN_COMP) or (isinstance(n, ast.Name) and n.id == acc)
               for p in parts for n in ast.walk(p)):
            return False
        targets = {n.id for n in ast.walk(st.target) if isinstance(n, ast.Name)}
        end = getattr(st, "end_lineno", None)
        fn = self.fn_stack[-1] if self.fn_stack else None
        if end is None or fn is None:
            return False
        later = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                 and getattr(n, "lineno", 0) > end}
        if targets & later:
            return False                     # loop variable is read after the loop -> not equivalent
        out[-1] = ast.Assign(targets=init.targets, lineno=init.lineno, value=ast.ListComp(
            elt=call.args[0], generators=[ast.comprehension(target=st.target, iter=st.iter, ifs=conds, is_async=0)]))
        self.note("loop-to-comprehension")
        return True


# ──────────────────────────── file-level driver ────────────────────────────
def _outer_functions(tree):
    found = []

    def walk(node):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(ch)
            else:
                walk(ch)
    walk(tree)
    return found


def _unused_imports(tree, lines):
    edits = []
    if any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets)
           for n in tree.body):
        return edits
    for st in tree.body:
        if not isinstance(st, (ast.Import, ast.ImportFrom)) or getattr(st, "module", None) == "__future__":
            continue
        if any(a.name == "*" for a in st.names) or "noqa" in "".join(lines[st.lineno - 1:st.end_lineno]):
            continue
        rest = "\n".join(l for i, l in enumerate(lines, 1) if not st.lineno <= i <= st.end_lineno)
        keep = []
        for a in st.names:
            name = (a.asname or a.name).split(".")[0]
            if re.search(rf"\b{re.escape(name)}\b", rest):
                keep.append(a)
        if len(keep) == len(st.names):
            continue
        if keep:
            new = copy.deepcopy(st)
            new.names = keep
            indent = lines[st.lineno - 1][:st.col_offset]
            edits.append((st.lineno, st.end_lineno, [indent + ast.unparse(new)], "unused-import"))
        else:
            edits.append((st.lineno, st.end_lineno, [], "unused-import"))
    return edits


def optimize_source(src, policy):
    """-> (new_src, notes{rule: n}, skipped[{function, reason}])  - never raises on valid Python."""
    marked, comments = mark_comments(src)
    try:
        tree = ast.parse(marked)
    except SyntaxError:
        marked, comments = src, {}
        tree = ast.parse(src)
    lines = src.splitlines()
    dq = src.count('"') > src.count("'")
    edits, notes, skipped = [], {}, []
    for fn in _outer_functions(tree):
        orig_seg = "\n".join(lines[fn.lineno - 1:fn.end_lineno])
        work = copy.deepcopy(fn)
        work.decorator_list = []
        rw = Rewriter()
        work = rw.visit(work)
        if not rw.notes:
            continue
        ast.fix_missing_locations(work)
        text = restore_comments(requote(ast.unparse(work), dq), comments).strip("\n")
        if count_comments(text) < count_comments(orig_seg) and not policy["allow_comment_loss"]:
            skipped.append({"function": fn.name, "reason": "has inline comments that a rewrite would drop"})
            continue
        indent = lines[fn.lineno - 1][:fn.col_offset]
        new_lines = [(indent + l if l.strip() else l) for l in text.split("\n")]
        edits.append((fn.lineno, fn.end_lineno, new_lines, rw.notes))
    if policy["remove_unused_imports"]:
        edits.extend(_unused_imports(ast.parse(src), lines))
    out = list(lines)
    for start, end, new, info in sorted(edits, key=lambda e: -e[0]):
        out[start - 1:end] = new
        for k, v in (info.items() if isinstance(info, dict) else [(info, 1)]):
            notes[k] = notes.get(k, 0) + v
    new_src = "\n".join(out) + ("\n" if src.endswith("\n") else "")
    if not src.startswith("\n"):
        new_src = new_src.lstrip("\n")
    try:
        ast.parse(new_src)
    except SyntaxError:                      # safety net: never emit broken code
        return src, {}, [{"function": "*", "reason": "rewrite produced invalid syntax - discarded"}]
    return new_src, notes, skipped
