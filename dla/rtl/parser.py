"""Verilog-2005 parser for the synthesizable subset -> AST (:class:`N` nodes).

    mods = parse_files(["rtl/top.v"])        # {name: N("module", ...)}

Every node carries kind, fields and the source position (file, line). Constructs
outside the subset raise ParseError("file:line: unsupported ...").

Node kinds (fields in order):
  module      name, params [N param], ports [name], items [N]
  param       name, expr, rng|None, signed, local
  decl        kind (input output inout wire reg integer genvar), names [(name, array_rng|None)],
              rng|None, signed, netkind (wire|reg|None)
  assign      lhs, rhs                          (continuous)
  always      sens (None = @* | [(edge|None, expr)]), stmt
  inst        module, name, params ([(name|None, expr)]), conns ([(name|None, expr|None)])
  genfor      var, init, cond, step_expr, label, items
  genif       cond, then_items, else_items
  genblock    label, items
  function    name, rng|None, signed, inputs [(name, rng, signed)], decls [N decl], stmt
  -- statements
  block       label, stmts, decls
  bassign / nbassign   lhs, rhs
  if          cond, then, else|None
  case        kind (case casez casex), expr, items [([exprs]|None, stmt)]
  for         var, init, cond, step_lhs, step_rhs, stmt
  null
  -- expressions
  num         width|None, signed, value, xmask, zmask   (unsized: width None -> 32 bits)
  id          name
  index       base, idx                      a[i]
  range       base, msb, lsb                 a[m:l]
  prange      base, start, width, up         a[s+:w] / a[s-:w]
  unop        op, e
  binop       op, a, b
  tern        c, a, b
  concat      [e]
  repl        count, [e]
  call        name, [args]                   user function or $system function
"""
import re

from ..db.lex import ParseError
from .lexer import lex_files, lex_text

KEYWORDS = set("""
    always and assign automatic begin buf bufif0 bufif1 case casex casez cell cmos config deassign
    default defparam design disable edge else end endcase endconfig endfunction endgenerate
    endmodule endprimitive endspecify endtable endtask event for force forever fork function
    generate genvar highz0 highz1 if ifnone incdir include initial inout input instance integer
    join large liblist library localparam macromodule medium module nand negedge nmos nor
    noshowcancelled not notif0 notif1 or output parameter pmos posedge primitive pull0 pull1
    pulldown pullup pulsestyle_onevent pulsestyle_ondetect rcmos real realtime reg release repeat
    rnmos rpmos rtran rtranif0 rtranif1 scalared showcancelled signed small specify specparam
    strong0 strong1 supply0 supply1 table task time tran tranif0 tranif1 tri tri0 tri1 triand
    trior trireg unsigned use uwire vectored wait wand weak0 weak1 while wire wor xnor xor
""".split())


class N:
    __slots__ = ("k", "a", "file", "line")

    def __init__(self, k, *a, at=None):
        self.k = k
        self.a = a
        self.file, self.line = (at.file, at.line) if at is not None else ("", 0)

    def __getitem__(self, i):
        return self.a[i]

    def __repr__(self):
        return "N(%s%s)" % (self.k, "".join(", %r" % (x,) for x in self.a))


def parse_number(text):
    """Verilog literal -> (width|None, signed, value, xmask, zmask)."""
    m = re.match(r"([0-9_]*)'([sS]?)([bBoOdDhH])([0-9a-fA-FxXzZ?_]+)$", text)
    if not m:
        return None, True, int(text.replace("_", "")), 0, 0
    width = int(m.group(1).replace("_", "")) if m.group(1).replace("_", "") else None
    signed = bool(m.group(2))
    base = m.group(3).lower()
    digits = m.group(4).replace("_", "").lower()
    val = xm = zm = 0
    if base == "d":
        if digits in ("x", "z", "?"):
            w = width or 32
            full = (1 << w) - 1
            return width, signed, 0, full if digits == "x" else 0, full if digits != "x" else 0
        val = int(digits)
    else:
        per = {"b": 1, "o": 3, "h": 4}[base]
        for ch in digits:
            val, xm, zm = val << per, xm << per, zm << per
            if ch == "x":
                xm |= (1 << per) - 1
            elif ch in "z?":
                zm |= (1 << per) - 1
            else:
                val |= int(ch, 16)
    if width is not None:
        mask = (1 << width) - 1
        # an x / z in the top digit extends to the full width
        top = len(digits) * {"b": 1, "o": 3, "h": 4, "d": 0}[base]
        if base != "d" and top < width and digits[0] in "xz?":
            ext = mask & ~((1 << top) - 1)
            if digits[0] == "x":
                xm |= ext
            else:
                zm |= ext
        val, xm, zm = val & mask, xm & mask, zm & mask
    return width, signed, val, xm, zm


class Parser:
    def __init__(self, toks):
        self.t = toks
        self.i = 0

    # ------------------------------------------------------------------ cursor
    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else None

    def pv(self, k=0):
        t = self.peek(k)
        return t.value if t is not None and t.kind in ("op", "id") else None

    def err(self, msg, tok=None):
        tok = tok or self.peek() or (self.t[-1] if self.t else None)
        if tok is None:
            return ParseError("<empty>: %s" % msg)
        return ParseError("%s:%d: %s" % (tok.file, tok.line, msg))

    def next(self):
        t = self.peek()
        if t is None:
            raise self.err("unexpected end of input")
        self.i += 1
        return t

    def expect(self, v):
        t = self.next()
        if t.value != v or t.kind not in ("op", "id"):
            self.i -= 1
            raise self.err("expected %r, got %r" % (v, t.value))
        return t

    def accept(self, v):
        t = self.peek()
        if t is not None and t.kind in ("op", "id") and t.value == v:
            self.i += 1
            return t
        return None

    def ident(self):
        t = self.next()
        if t.kind == "eid" or (t.kind == "id" and t.value not in KEYWORDS):
            return t.value
        self.i -= 1
        raise self.err("expected an identifier, got %r" % t.value)

    def is_ident(self, k=0):
        t = self.peek(k)
        return t is not None and (t.kind == "eid" or (t.kind == "id" and t.value not in KEYWORDS))

    # ------------------------------------------------------------------ modules
    def source(self):
        mods = {}
        while self.peek() is not None:
            t = self.peek()
            if t.value in ("module", "macromodule"):
                m = self.module()
                if m[0] in mods:
                    raise self.err("module %s defined twice" % m[0], t)
                mods[m[0]] = m
            else:
                raise self.err("unsupported top-level item %r" % t.value)
        return mods

    def module(self):
        at = self.next()
        name = self.ident()
        params, ports, items = [], [], []
        if self.accept("#"):
            self.expect("(")
            while not self.accept(")"):
                self.accept(",")
                if self.pv() in ("parameter", "localparam"):
                    self.next()
                params += self.param_assigns(local=False, header=True)
        if self.accept("("):
            if not self.accept(")"):
                if self.pv() in ("input", "output", "inout"):
                    while True:
                        d = self.port_decl_ansi()
                        items.append(d)
                        ports += [n for n, _ in d[1]]
                        if self.accept(")"):
                            break
                        self.expect(",")
                        if self.pv() not in ("input", "output", "inout"):
                            # continuation of the previous declaration: same direction / type
                            nm = self.ident()
                            items.append(N("decl", d[0], [(nm, None)], d[2], d[3], d[4], at=self.t[self.i - 1]))
                            ports.append(nm)
                            while self.accept(","):
                                if self.pv() in ("input", "output", "inout"):
                                    self.i -= 1
                                    break
                                nm = self.ident()
                                items.append(N("decl", d[0], [(nm, None)], d[2], d[3], d[4], at=self.t[self.i - 1]))
                                ports.append(nm)
                            if self.accept(")"):
                                break
                            self.expect(",")
                else:
                    while True:
                        if self.pv() == ".":
                            raise self.err("unsupported: explicit port expressions")
                        ports.append(self.ident())
                        if self.accept(")"):
                            break
                        self.expect(",")
        self.expect(";")
        while self.pv() != "endmodule":
            if self.peek() is None:
                raise self.err("missing endmodule for module %s" % name, at)
            items += self.module_item()
        self.next()
        return N("module", name, params, ports, items, at=at)

    def port_decl_ansi(self):
        at = self.next()
        kind = at.value
        netkind = None
        if self.pv() in ("wire", "reg"):
            netkind = self.next().value
        signed = bool(self.accept("signed"))
        rng = self.range_opt()
        name = self.ident()
        return N("decl", kind, [(name, None)], rng, signed, netkind, at=at)

    def range_opt(self):
        if self.pv() != "[":
            return None
        self.next()
        msb = self.expr()
        self.expect(":")
        lsb = self.expr()
        self.expect("]")
        return (msb, lsb)

    def param_assigns(self, local, header=False):
        out = []
        signed = bool(self.accept("signed"))
        if self.accept("integer"):
            signed = True
        rng = self.range_opt()
        while True:
            at = self.peek()
            name = self.ident()
            self.expect("=")
            e = self.expr()
            out.append(N("param", name, e, rng, signed, local, at=at))
            if header:
                if self.pv() == "," and self.pv(1) not in ("parameter", "localparam") and self.is_ident(1) \
                        and self.pv(2) == "=":
                    self.next()
                    continue
                return out
            if not self.accept(","):
                break
        self.expect(";")
        return out

    def module_item(self):
        t = self.peek()
        v = t.value if t.kind == "id" else None
        if v in ("input", "output", "inout", "wire", "reg", "integer", "genvar", "tri", "supply0", "supply1"):
            return self.decl()
        if v in ("parameter", "localparam"):
            self.next()
            return self.param_assigns(local=(v == "localparam"))
        if v == "assign":
            self.next()
            out = []
            if self.pv() == "#":
                raise self.err("unsupported: delays")
            while True:
                lhs = self.lvalue()
                self.expect("=")
                out.append(N("assign", lhs, self.expr(), at=t))
                if not self.accept(","):
                    break
            self.expect(";")
            return out
        if v == "always":
            self.next()
            if not self.accept("@"):
                raise self.err("unsupported: always without event control")
            sens = self.sensitivity()
            return [N("always", sens, self.stmt(), at=t)]
        if v == "generate":
            self.next()
            items = []
            while not self.accept("endgenerate"):
                items += self.module_item()
            return items
        if v == "for":
            return [self.gen_for()]
        if v == "if":
            return [self.gen_if()]
        if v == "begin":
            self.next()
            label = self.ident() if self.accept(":") else None
            items = []
            while not self.accept("end"):
                items += self.module_item()
            return [N("genblock", label, items, at=t)]
        if v == "function":
            return [self.function()]
        if v in ("initial", "task", "defparam", "specify", "primitive", "case", "assert"):
            raise self.err("unsupported: %s" % v)
        if v in ("and", "or", "nand", "nor", "xor", "xnor", "not", "buf"):
            return self.gate_prims()
        if self.is_ident():
            return self.instances()
        if t.kind == "op" and t.value == ";":
            self.next()
            return []
        raise self.err("unsupported module item %r" % t.value)

    def decl(self):
        at = self.next()
        kind = at.value
        if kind in ("tri",):
            kind = "wire"
        if kind in ("supply0", "supply1"):
            raise self.err("unsupported: %s" % kind, at)
        netkind = None
        if kind in ("input", "output", "inout") and self.pv() in ("wire", "reg"):
            netkind = self.next().value
        signed = bool(self.accept("signed"))
        rng = None if kind in ("integer", "genvar") else self.range_opt()
        if kind == "integer":
            signed = True
        names, out = [], []
        while True:
            nat = self.peek()
            name = self.ident()
            arr = self.range_opt()
            if self.pv() == "[":
                raise self.err("unsupported: multi-dimensional arrays")
            names.append((name, arr))
            if self.accept("="):
                if kind != "wire":
                    raise self.err("unsupported: initial value on %s" % kind, nat)
                out.append(N("assign", N("id", name, at=nat), self.expr(), at=nat))
            if not self.accept(","):
                break
        self.expect(";")
        return [N("decl", kind, names, rng, signed, netkind, at=at)] + out

    def gate_prims(self):
        at = self.next()
        g = at.value
        out = []
        while True:
            if self.is_ident():
                self.ident()
            self.expect("(")
            args = [self.expr()]
            while self.accept(","):
                args.append(self.expr())
            self.expect(")")
            if len(args) < 2:
                raise self.err("gate %s needs an output and inputs" % g, at)
            lhs, ins = args[0], args[1:]
            if g in ("not", "buf"):
                e = ins[0] if g == "buf" else N("unop", "~", ins[0], at=at)
            else:
                base = {"and": "&", "nand": "&", "or": "|", "nor": "|", "xor": "^", "xnor": "^"}[g]
                e = ins[0]
                for x in ins[1:]:
                    e = N("binop", base, e, x, at=at)
                if g in ("nand", "nor", "xnor"):
                    e = N("unop", "~", e, at=at)
            out.append(N("assign", lhs, e, at=at))
            if not self.accept(","):
                break
        self.expect(";")
        return out

    def instances(self):
        at = self.peek()
        mod = self.ident()
        params = []
        if self.accept("#"):
            self.expect("(")
            params = self.connections()
        out = []
        while True:
            iat = self.peek()
            name = self.ident()
            if self.pv() == "[":
                raise self.err("unsupported: instance arrays")
            self.expect("(")
            conns = self.connections()
            out.append(N("inst", mod, name, params, conns, at=iat))
            if not self.accept(","):
                break
        self.expect(";")
        return out

    def connections(self):
        """(...) after '(' : named .p(e) or positional; returns [(name|None, expr|None)]."""
        out = []
        if self.accept(")"):
            return out
        while True:
            if self.accept("."):
                name = self.ident()
                self.expect("(")
                e = None if self.pv() == ")" else self.expr()
                self.expect(")")
                out.append((name, e))
            elif self.pv() in (",", ")"):
                out.append((None, None))
            else:
                out.append((None, self.expr()))
            if self.accept(")"):
                return out
            self.expect(",")

    def gen_for(self):
        at = self.next()
        self.expect("(")
        var = self.ident()
        self.expect("=")
        init = self.expr()
        self.expect(";")
        cond = self.expr()
        self.expect(";")
        v2 = self.ident()
        if v2 != var:
            raise self.err("generate loop must step its own variable %s" % var)
        self.expect("=")
        step = self.expr()
        self.expect(")")
        if self.pv() == "begin":
            self.next()
            label = self.ident() if self.accept(":") else None
            items = []
            while not self.accept("end"):
                items += self.module_item()
        else:
            label, items = None, self.module_item()
        return N("genfor", var, init, cond, step, label, items, at=at)

    def gen_if(self):
        at = self.next()
        self.expect("(")
        cond = self.expr()
        self.expect(")")
        then = self.gen_body()
        els = []
        if self.accept("else"):
            els = [self.gen_if()] if self.pv() == "if" else self.gen_body()
        return N("genif", cond, then, els, at=at)

    def gen_body(self):
        if self.pv() == "begin":
            at = self.next()
            label = self.ident() if self.accept(":") else None
            items = []
            while not self.accept("end"):
                items += self.module_item()
            return [N("genblock", label, items, at=at)]
        return self.module_item()

    def function(self):
        at = self.next()
        self.accept("automatic")
        signed = bool(self.accept("signed"))
        rng = None
        if self.accept("integer"):
            signed, rng = True, (N("num", None, True, 31, 0, 0, at=at), N("num", None, True, 0, 0, 0, at=at))
        else:
            rng = self.range_opt()
        name = self.ident()
        inputs, decls = [], []
        if self.accept("("):
            while not self.accept(")"):
                self.accept(",")
                self.expect("input")
                self.accept("reg")
                s = bool(self.accept("signed"))
                r = self.range_opt()
                inputs.append((self.ident(), r, s))
                while self.pv() == "," and self.is_ident(1):
                    self.next()
                    inputs.append((self.ident(), r, s))
        self.expect(";")
        while self.pv() in ("input", "reg", "integer"):
            d = self.decl()[0]
            if d[0] == "input":
                inputs += [(n, d[2], d[3]) for n, _ in d[1]]
            else:
                decls.append(d)
        body = self.stmt()
        self.expect("endfunction")
        return N("function", name, rng, signed, inputs, decls, body, at=at)

    # ------------------------------------------------------------------ statements
    def sensitivity(self):
        if self.accept("*"):
            return None
        self.expect("(")
        if self.accept("*"):
            self.expect(")")
            return None
        items = []
        while True:
            edge = None
            if self.pv() in ("posedge", "negedge"):
                edge = self.next().value[:3]
            items.append((edge, self.expr()))
            if self.accept(")"):
                return items
            if not (self.accept("or") or self.accept(",")):
                raise self.err("expected 'or' or ',' in sensitivity list")

    def stmt(self):
        t = self.peek()
        v = t.value if t.kind in ("id", "op") else None
        if v == "begin":
            self.next()
            label = self.ident() if self.accept(":") else None
            stmts, decls = [], []
            while not self.accept("end"):
                if self.pv() in ("reg", "integer"):
                    decls += [d for d in self.decl() if d.k == "decl"]
                    continue
                stmts.append(self.stmt())
            return N("block", label, stmts, decls, at=t)
        if v == "if":
            self.next()
            self.expect("(")
            c = self.expr()
            self.expect(")")
            a = self.stmt()
            b = self.stmt() if self.accept("else") else None
            return N("if", c, a, b, at=t)
        if v in ("case", "casez", "casex"):
            self.next()
            self.expect("(")
            e = self.expr()
            self.expect(")")
            items = []
            while not self.accept("endcase"):
                if self.accept("default"):
                    self.accept(":")
                    items.append((None, self.stmt()))
                    continue
                labels = [self.expr()]
                while self.accept(","):
                    labels.append(self.expr())
                self.expect(":")
                items.append((labels, self.stmt()))
            return N("case", v, e, items, at=t)
        if v == "for":
            self.next()
            self.expect("(")
            var = self.ident()
            self.expect("=")
            init = self.expr()
            self.expect(";")
            cond = self.expr()
            self.expect(";")
            slhs = self.lvalue()
            self.expect("=")
            srhs = self.expr()
            self.expect(")")
            return N("for", var, init, cond, slhs, srhs, self.stmt(), at=t)
        if v == ";":
            self.next()
            return N("null", at=t)
        if t.kind == "sysid":
            self.next()                       # $display, $finish, ...: no hardware
            if self.accept("("):
                depth = 1
                while depth:
                    x = self.next()
                    if x.kind == "op" and x.value == "(":
                        depth += 1
                    elif x.kind == "op" and x.value == ")":
                        depth -= 1
            self.expect(";")
            return N("null", at=t)
        if v in ("while", "repeat", "forever", "fork", "wait", "disable", "@", "#", "assign", "force"):
            raise self.err("unsupported statement %r" % v)
        lhs = self.lvalue()
        if self.accept("="):
            kind = "bassign"
        elif self.accept("<="):
            kind = "nbassign"
        else:
            raise self.err("expected '=' or '<='")
        if self.pv() == "#":
            raise self.err("unsupported: delays")
        rhs = self.expr()
        self.expect(";")
        return N(kind, lhs, rhs, at=t)

    def lvalue(self):
        t = self.peek()
        if self.accept("{"):
            items = [self.lvalue()]
            while self.accept(","):
                items.append(self.lvalue())
            self.expect("}")
            return N("concat", items, at=t)
        name = self.ident()
        return self.selects(N("id", name, at=t))

    def selects(self, base):
        while self.pv() == "[":
            t = self.next()
            a = self.expr()
            if self.accept(":"):
                b = self.expr()
                self.expect("]")
                base = N("range", base, a, b, at=t)
            elif self.pv() in ("+:", "-:"):
                up = self.next().value == "+:"
                w = self.expr()
                self.expect("]")
                base = N("prange", base, a, w, up, at=t)
            else:
                self.expect("]")
                base = N("index", base, a, at=t)
        return base

    # ------------------------------------------------------------------ expressions
    _BIN = [
        ("||",), ("&&",), ("|",), ("^", "~^", "^~"), ("&",), ("==", "!=", "===", "!=="),
        ("<", "<=", ">", ">="), ("<<", ">>", "<<<", ">>>"), ("+", "-"), ("*", "/", "%"), ("**",),
    ]

    def expr(self):
        t = self.peek()
        c = self.binary(0)
        if self.accept("?"):
            a = self.expr()
            self.expect(":")
            b = self.expr()
            return N("tern", c, a, b, at=t)
        return c

    def binary(self, level):
        if level == len(self._BIN):
            return self.unary()
        e = self.binary(level + 1)
        ops = self._BIN[level]
        while True:
            t = self.peek()
            if t is None or t.kind != "op" or t.value not in ops:
                return e
            self.next()
            rhs = self.binary(level + 1) if t.value != "**" else self.unary()
            e = N("binop", "~^" if t.value == "^~" else t.value, e, rhs, at=t)

    def unary(self):
        t = self.peek()
        if t is not None and t.kind == "op" and t.value in ("+", "-", "!", "~", "&", "~&", "|", "~|", "^", "~^", "^~"):
            self.next()
            return N("unop", "~^" if t.value == "^~" else t.value, self.unary(), at=t)
        return self.primary()

    def primary(self):
        t = self.next()
        if t.kind == "num":
            return N("num", *parse_number(t.value), at=t)
        if t.kind == "str":
            raise self.err("unsupported: string literals", t)
        if t.kind == "sysid":
            args = []
            if self.accept("("):
                if not self.accept(")"):
                    args.append(self.expr())
                    while self.accept(","):
                        args.append(self.expr())
                    self.expect(")")
            return N("call", t.value, args, at=t)
        if t.kind == "op" and t.value == "(":
            e = self.expr()
            self.expect(")")
            return e
        if t.kind == "op" and t.value == "{":
            first = self.expr()
            if self.accept("{"):                      # replication {n{...}}
                items = [self.expr()]
                while self.accept(","):
                    items.append(self.expr())
                self.expect("}")
                self.expect("}")
                return N("repl", first, items, at=t)
            items = [first]
            while self.accept(","):
                items.append(self.expr())
            self.expect("}")
            return N("concat", items, at=t)
        if t.kind == "eid" or (t.kind == "id" and t.value not in KEYWORDS):
            if self.pv() == "(":
                self.next()
                args = []
                if not self.accept(")"):
                    args.append(self.expr())
                    while self.accept(","):
                        args.append(self.expr())
                    self.expect(")")
                return N("call", t.value, args, at=t)
            if self.pv() == ".":
                raise self.err("unsupported: hierarchical references")
            return self.selects(N("id", t.value, at=t))
        raise self.err("unexpected %r in expression" % t.value, t)


def parse_files(paths, defines=None, include_dirs=()):
    return Parser(lex_files(paths, defines, include_dirs)).source()


def parse_text(text, fname="<string>", defines=None):
    return Parser(lex_text(text, fname, defines)).source()
