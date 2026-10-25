"""Verilog-2005 lexer with a small preprocessor.

    toks = lex_files(["rtl/top.v"], defines={"WIDTH": "8"})
    # [Tok(kind, value, file, line)]  kind: id | sysid | num | str | op

Preprocessor: `define NAME text (no arguments), `undef, `ifdef / `ifndef / `elsif /
`else / `endif, `include "file" (relative to the including file, then include dirs);
`timescale, `default_nettype, `resetall, `celldefine and `endcelldefine are ignored.
Macros with arguments and other directives stop with file:line errors.
"""
import os
import re

from ..db.lex import ParseError


class Tok:
    __slots__ = ("kind", "value", "file", "line", "pos")

    def __init__(self, kind, value, file, line, pos=-1):
        self.kind, self.value, self.file, self.line, self.pos = kind, value, file, line, pos

    def __repr__(self):
        return "Tok(%s %r @%d)" % (self.kind, self.value, self.line)


OPS = sorted("""
    <<< >>> === !== <<= >>= ~& ~| ~^ ^~ << >> <= >= == != && || ** -: +: ->
    + - * / % < > ! ~ & | ^ ? : ; , . ( ) [ ] { } # @ = '
""".split(), key=len, reverse=True)

_TOKEN = re.compile(r"""
    (?P<ws>[ \t\r\f]+) |
    (?P<nl>\n) |
    (?P<lcomment>//[^\n]*) |
    (?P<bcomment>/\*.*?\*/) |
    (?P<attr>\(\*(?!\)).*?\*\)) |
    (?P<str>"(?:[^"\\\n]|\\.)*") |
    (?P<num>(?:[0-9][0-9_]*\s*)?'[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ?_]+ | [0-9][0-9_]*(?:\.[0-9_]+)?(?:[eE][-+]?[0-9]+)?) |
    (?P<directive>`[A-Za-z_][A-Za-z0-9_$]*) |
    (?P<sysid>\$[A-Za-z_][A-Za-z0-9_$]*) |
    (?P<id>[A-Za-z_][A-Za-z0-9_$]*) |
    (?P<esc>\\[^\s]+) |
    (?P<op>""" + "|".join(re.escape(o) for o in OPS) + r""")
""", re.S | re.X)

_IGNORED = {"timescale", "default_nettype", "resetall", "celldefine", "endcelldefine",
            "nounconnected_drive", "unconnected_drive"}


def _raw_tokens(text, fname):
    """Tokens incl. directives, with lines; comments removed."""
    out = []
    pos, line, n = 0, 1, len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            raise ParseError("%s:%d: unexpected character %r" % (fname, line, text[pos]))
        k = m.lastgroup
        v = m.group(k)
        if k == "nl":
            out.append(Tok("nl", "\n", fname, line))
        elif k not in ("ws", "lcomment", "bcomment", "attr"):
            if k == "num":
                v = re.sub(r"\s+", "", v)
                if re.match(r"[0-9_]+\.|[0-9_]+[eE]", v):
                    raise ParseError("%s:%d: real numbers are not supported" % (fname, line))
            elif k == "esc":
                k, v = "eid", v[1:]
            out.append(Tok(k, v, fname, line, m.start()))
        line += v.count("\n") if k != "nl" else 1
        pos = m.end()
    return out


def lex_text(text, fname="<string>", defines=None, include_dirs=(), _depth=0):
    defines = dict(defines or {})
    toks = _preprocess(_raw_tokens(text, fname), fname, defines, list(include_dirs), _depth)
    return toks


def lex_files(paths, defines=None, include_dirs=()):
    defines = dict(defines or {})
    out = []
    for p in paths:
        with open(p) as f:
            out += _preprocess(_raw_tokens(f.read(), p), p, defines, list(include_dirs), 0)
    return out


def _preprocess(raw, fname, defines, incdirs, depth):
    if depth > 20:
        raise ParseError("%s: `include nesting too deep" % fname)
    out = []
    stack = []                    # [active, taken] per `ifdef level
    i = 0

    def active():
        return all(a for a, _ in stack)

    def rest_of_line(j):
        vals = []
        while j < len(raw) and raw[j].kind != "nl":
            vals.append(raw[j])
            j += 1
        return vals, j

    while i < len(raw):
        t = raw[i]
        if t.kind == "nl":
            i += 1
            continue
        if t.kind == "directive":
            d = t.value[1:]
            if d in ("ifdef", "ifndef", "elsif"):
                name = raw[i + 1].value if i + 1 < len(raw) else None
                if name is None or raw[i + 1].kind != "id":
                    raise ParseError("%s:%d: `%s needs a macro name" % (t.file, t.line, d))
                cond = (name in defines) == (d != "ifndef")
                if d == "elsif":
                    if not stack:
                        raise ParseError("%s:%d: `elsif without `ifdef" % (t.file, t.line))
                    taken = stack[-1][1]
                    stack[-1] = [cond and not taken, taken or cond]
                else:
                    stack.append([cond, cond])
                i += 2
                continue
            if d == "else":
                if not stack:
                    raise ParseError("%s:%d: `else without `ifdef" % (t.file, t.line))
                stack[-1] = [not stack[-1][1], True]
                i += 1
                continue
            if d == "endif":
                if not stack:
                    raise ParseError("%s:%d: `endif without `ifdef" % (t.file, t.line))
                stack.pop()
                i += 1
                continue
            if not active():
                i += 1
                continue
            if d == "define":
                vals, j = rest_of_line(i + 1)
                if not vals or vals[0].kind != "id":
                    raise ParseError("%s:%d: `define needs a name" % (t.file, t.line))
                if len(vals) > 1 and vals[1].value == "(" and vals[1].pos == vals[0].pos + len(vals[0].value):
                    raise ParseError("%s:%d: macros with arguments are not supported" % (t.file, t.line))
                defines[vals[0].value] = vals[1:]
                i = j
                continue
            if d == "undef":
                defines.pop(raw[i + 1].value, None)
                i += 2
                continue
            if d == "include":
                s = raw[i + 1] if i + 1 < len(raw) else None
                if s is None or s.kind != "str":
                    raise ParseError("%s:%d: `include needs a file name" % (t.file, t.line))
                name = s.value[1:-1]
                cands = [os.path.join(os.path.dirname(fname), name)] + [os.path.join(d_, name) for d_ in incdirs]
                path = next((c for c in cands if os.path.isfile(c)), None)
                if path is None:
                    raise ParseError("%s:%d: include file %s not found" % (t.file, t.line, name))
                with open(path) as f:
                    out += _preprocess(_raw_tokens(f.read(), path), path, defines, incdirs, depth + 1)
                i += 2
                continue
            if d in _IGNORED:
                _, i = rest_of_line(i + 1) if d in ("timescale", "default_nettype", "unconnected_drive") else (None, i + 1)
                continue
            if d in defines:
                body = defines[d]
                out += [Tok(b.kind, b.value, t.file, t.line) for b in _expand(body, defines, t)]
                i += 1
                continue
            raise ParseError("%s:%d: unknown directive or macro `%s" % (t.file, t.line, d))
        if active():
            out.append(t)
        i += 1
    if stack:
        raise ParseError("%s: missing `endif" % fname)
    return out


def _expand(body, defines, at, depth=0):
    if depth > 50:
        raise ParseError("%s:%d: macro expansion too deep" % (at.file, at.line))
    out = []
    for b in body:
        if b.kind == "directive" and b.value[1:] in defines:
            out += _expand(defines[b.value[1:]], defines, at, depth + 1)
        else:
            out.append(b)
    return out
