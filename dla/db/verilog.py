"""Structural (gate-level) Verilog read / write for flat netlists.

    d = read_verilog("synth.v", top="counter8")      # -> Design (netlist only)
    write_verilog(d, "out.v")

Accepted: module with ANSI or non-ANSI ports, input/output/inout/wire declarations
with ranges, `assign lhs = rhs;` between nets, bits, part-selects, concatenations,
replications and sized constants, cell instances with named connections
(`.A(n)`, `.B()`), `(* attributes *)` (ignored). Buses are bit-blasted ("a[3]"); declared wires
without connections are dropped (as in DEF);
assign aliases are merged (a port name wins as the net name, a constant wins over
everything). Anything else stops with `file:line: unsupported ...`.
Cells instantiating another module of the same file (hierarchy) are rejected: the
netlist must be flat (flattening comes with the RTL front end).
"""
import re

from .design import CONSTS, PIN, Design
from .lex import ParseError

_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*$")
_BIT = re.compile(r"(.*)\[(-?\d+)\]$")
_KEYWORDS = {"module", "endmodule", "input", "output", "inout", "wire", "assign", "reg",
             "supply0", "supply1", "tri", "parameter", "localparam", "defparam", "always",
             "initial", "generate", "endgenerate", "function", "task", "specify", "integer"}


# ============================================================================= lexer
def _tokenize(text, fname):
    """[(kind, value, line)] kind: id | num | sym; comments and attributes removed."""
    out = []
    i, n, ln = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            ln += 1
            i += 1
        elif c.isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i) or text.startswith("(*", i):
            end = "*/" if c == "/" else "*)"
            j = text.find(end, i + 2)
            if j < 0:
                raise ParseError("%s:%d: unterminated comment/attribute" % (fname, ln))
            ln += text.count("\n", i, j)
            i = j + 2
        elif c == "\\":                                     # escaped identifier
            j = i + 1
            while j < n and not text[j].isspace():
                j += 1
            out.append(("id", _escape(text[i + 1:j]), ln))
            i = j
        elif c.isalpha() or c == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            out.append(("id", text[i:j], ln))
            i = j
        elif c.isdigit() or c == "'":
            m = re.compile(r"(\d*)\s*'\s*([sS]?[bBoOdDhH])\s*([0-9a-fA-FxXzZ_?]+)|(\d+)").match(text, i)
            if not m:
                raise ParseError("%s:%d: bad number" % (fname, ln))
            out.append(("num", m.group(0), ln))
            i = m.end()
        elif c == "`":
            raise ParseError("%s:%d: unsupported compiler directive" % (fname, ln))
        else:
            out.append(("sym", c, ln))
            i += 1
    return out


def _escape(raw):
    """Escaped Verilog identifier -> internal name (brackets escaped as in DEF)."""
    return raw.replace("[", "\\[").replace("]", "\\]")


def _unescape(name):
    return name.replace("\\[", "[").replace("\\]", "]")


def _const_bits(tok, width_hint=None):
    """Sized constant -> bits MSB first ("1'b0", ...)."""
    m = re.match(r"(\d*)\s*'\s*[sS]?([bBoOdDhH])\s*([0-9a-fA-FxXzZ_?]+)$", tok)
    if not m:
        val, size = int(tok), 32
        return ["1'b%d" % ((val >> k) & 1) for k in range(size - 1, -1, -1)]
    size = int(m.group(1)) if m.group(1) else 32
    base = m.group(2).lower()
    digits = m.group(3).replace("_", "").lower()
    bits = []
    if base == "d":
        if any(ch in "xz?" for ch in digits):
            bits = [digits[0].replace("?", "z")] * size
        else:
            v = int(digits)
            bits = [str((v >> k) & 1) for k in range(size - 1, -1, -1)]
    else:
        per = {"b": 1, "o": 3, "h": 4}[base]
        for ch in digits:
            if ch in "xz?":
                bits += [ch.replace("?", "z")] * per
            else:
                bits += list(format(int(ch, 16), "0%db" % per))
    if len(bits) < size:
        pad = bits[0] if bits and bits[0] in "xz" else "0"
        bits = [pad] * (size - len(bits)) + bits
    bits = bits[len(bits) - size:]
    return ["1'b" + b for b in bits]


# ============================================================================= parser
class _Module:
    def __init__(self, name, line):
        self.name, self.line = name, line
        self.port_order = []            # names as in the header
        self.decl = {}                  # name -> (kind, msb, lsb)  kind: input/output/inout/wire
        self.decl_order = []
        self.assigns = []               # (lhs bits, rhs bits, line)
        self.cells = []                 # (cell, inst, [(pin, bits)], line)


class _Parser:
    def __init__(self, text, fname):
        self.toks = _tokenize(text, fname)
        self.i = 0
        self.fname = fname

    def err(self, msg, line=None):
        if line is None:
            line = self.toks[min(self.i, len(self.toks) - 1)][2] if self.toks else 0
        return ParseError("%s:%d: %s" % (self.fname, line, msg))

    def peek(self, k=0):
        j = self.i + k
        return self.toks[j][1] if j < len(self.toks) else None

    def next(self):
        if self.i >= len(self.toks):
            raise self.err("unexpected end of file")
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, v):
        t = self.next()
        if t[1] != v:
            self.i -= 1
            raise self.err("expected %r, got %r" % (v, t[1]))
        return t

    def ident(self):
        t = self.next()
        if t[0] != "id" or t[1] in _KEYWORDS:
            self.i -= 1
            raise self.err("expected an identifier, got %r" % t[1])
        return t[1]

    def line(self):
        return self.toks[min(self.i, len(self.toks) - 1)][2]

    # ------------------------------------------------------------------ structure
    def modules(self):
        mods = {}
        while self.i < len(self.toks):
            t = self.next()
            if t[1] != "module":
                raise self.err("unsupported top-level item %r" % t[1], t[2])
            m = self.module(t[2])
            if m.name in mods:
                raise self.err("module %s defined twice" % m.name, t[2])
            mods[m.name] = m
        return mods

    def range_(self):
        """Optional [msb:lsb] -> (msb, lsb) or None."""
        if self.peek() != "[":
            return None
        self.next()
        msb = self.intval()
        self.expect(":")
        lsb = self.intval()
        self.expect("]")
        return msb, lsb

    def intval(self):
        t = self.next()
        neg = False
        if t[1] == "-":
            neg, t = True, self.next()
        if t[0] != "num" or "'" in t[1]:
            self.i -= 1
            raise self.err("expected an integer, got %r" % t[1])
        return -int(t[1]) if neg else int(t[1])

    def declare(self, m, kind, name, rng, line):
        msb, lsb = rng if rng else (None, None)
        old = m.decl.get(name)
        if old:
            if kind == "wire" and old[0] != "wire":
                if rng and (old[1], old[2]) != (msb, lsb):
                    raise self.err("range of %s differs from its port declaration" % name, line)
                return
            if old[0] == "wire" and kind != "wire":
                m.decl[name] = (kind, old[1], old[2]) if not rng else (kind, msb, lsb)
                return
            raise self.err("%s declared twice" % name, line)
        m.decl[name] = (kind, msb, lsb)
        m.decl_order.append(name)

    def module(self, line):
        m = _Module(self.ident(), line)
        if self.peek() == "#":
            raise self.err("unsupported: module parameters")
        if self.peek() == "(":
            self.next()
            kind, rng = None, None
            while self.peek() != ")":
                if self.peek() in ("input", "output", "inout"):
                    kind = self.next()[1]
                    if self.peek() in ("wire", "reg"):
                        if self.next()[1] == "reg":
                            raise self.err("unsupported: reg in a structural netlist")
                    rng = self.range_()
                name = self.ident()
                m.port_order.append(name)
                if kind:
                    self.declare(m, kind, name, rng, self.line())
                if self.peek() == ",":
                    self.next()
            self.next()
        self.expect(";")
        while True:
            t = self.next()
            v = t[1]
            if v == "endmodule":
                return m
            if v in ("input", "output", "inout", "wire"):
                if v != "wire" and self.peek() == "wire":
                    self.next()
                rng = self.range_()
                while True:
                    self.declare(m, v, self.ident(), rng, t[2])
                    if self.next()[1] == ";":
                        break
                    self.i -= 1
                    self.expect(",")
            elif v == "assign":
                while True:
                    lhs = self.expr(m)
                    self.expect("=")
                    rhs = self.expr(m)
                    m.assigns.append((lhs, rhs, t[2]))
                    if self.next()[1] == ";":
                        break
                    self.i -= 1
                    self.expect(",")
            elif t[0] == "id" and v not in _KEYWORDS:
                self.cell(m, v, t[2])
            else:
                raise self.err("unsupported statement %r" % v, t[2])

    def cell(self, m, cell, line):
        if self.peek() == "#":
            raise self.err("unsupported: instance parameters", line)
        inst = self.ident()
        if self.peek() == "[":
            raise self.err("unsupported: instance arrays", line)
        self.expect("(")
        conns = []
        while self.peek() != ")":
            if self.peek() != ".":
                raise self.err("unsupported: positional connections (instance %s)" % inst)
            self.next()
            pin = self.ident()
            self.expect("(")
            bits = [] if self.peek() == ")" else self.expr(m)
            self.expect(")")
            conns.append((pin, bits))
            if self.peek() == ",":
                self.next()
        self.next()
        self.expect(";")
        m.cells.append((cell, inst, conns, line))

    # ------------------------------------------------------------------ expressions
    def expr(self, m):
        """Bits MSB first; a bit is a (net name, index or None) pair resolved later, or a constant."""
        t = self.next()
        if t[1] == "{":
            if self.toks[self.i][0] == "num" and self.peek(1) == "{":      # replication {n{...}}
                n = int(self.next()[1])
                self.expect("{")
                inner = self.expr_list(m)
                self.expect("}")
                self.expect("}")
                return inner * n
            bits = self.expr_list(m)
            self.expect("}")
            return bits
        if t[0] == "num":
            return _const_bits(t[1])
        if t[0] != "id" or t[1] in _KEYWORDS:
            raise self.err("unsupported expression %r" % t[1], t[2])
        name = t[1]
        if self.peek() == "[":
            self.next()
            a = self.intval()
            if self.peek() == ":":
                self.next()
                b = self.intval()
                self.expect("]")
                step = -1 if a >= b else 1
                return [("ref", name, k) for k in range(a, b + step, step)]
            self.expect("]")
            return [("ref", name, a)]
        return [("ref", name, None)]

    def expr_list(self, m):
        bits = []
        while True:
            bits += self.expr(m)
            if self.peek() != ",":
                return bits
            self.next()


# ============================================================================= build
class _UF:
    def __init__(self):
        self.p = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[b] = a


def _bits_of(m, name):
    kind, msb, lsb = m.decl.get(name, ("wire", None, None))
    if msb is None:
        return [name]
    step = -1 if msb >= lsb else 1
    return ["%s[%d]" % (name, k) for k in range(msb, lsb + step, step)]


def _resolve(m, bits, fname, line):
    out = []
    for b in bits:
        if isinstance(b, str):
            out.append(b)
            continue
        _, name, idx = b
        decl = m.decl.get(name)
        if idx is None:
            out += _bits_of(m, name)
        else:
            if decl is None or decl[1] is None:
                raise ParseError("%s:%d: %s is not a bus" % (fname, line, name))
            lo, hi = sorted((decl[1], decl[2]))
            if not lo <= idx <= hi:
                raise ParseError("%s:%d: %s[%d] out of range" % (fname, line, name, idx))
            out.append("%s[%d]" % (name, idx))
    return out


def _build(m, fname, mods):
    d = Design(m.name)
    for name in m.port_order:
        if name not in m.decl or m.decl[name][0] == "wire":
            raise ParseError("%s:%d: port %s has no direction" % (fname, m.line, name))
    for name in m.port_order:                 # port ranges are interface; wire ranges are not kept
        kind, msb, lsb = m.decl[name]
        if msb is not None:
            d.buses[name] = (msb, lsb)
    uf = _UF()
    rank = {}
    for k, c in enumerate(CONSTS):
        rank[c] = (0, k)
    port_bits = []
    for name in m.port_order:
        kind = m.decl[name][0]
        for b in _bits_of(m, name):
            port_bits.append((b, kind.upper()))
            rank[b] = (1 if kind == "input" else 2, len(port_bits))
    for lhs, rhs, line in m.assigns:
        lb, rb = _resolve(m, lhs, fname, line), _resolve(m, rhs, fname, line)
        if any(b in CONSTS for b in lb):
            raise ParseError("%s:%d: constant on the left of an assign" % (fname, line))
        if len(rb) < len(lb):
            raise ParseError("%s:%d: assign width mismatch (%d vs %d)" % (fname, line, len(lb), len(rb)))
        for a, b in zip(lb, rb[len(rb) - len(lb):]):
            uf.union(a, b)

    def rep(b):
        return uf.find(b)

    # name every alias class after its best member: constant > input port > other port > first seen
    best = {}
    order = 0
    for b in list(uf.p):
        r = uf.find(b)
        key = rank.get(b, (3, order))
        order += 1
        if r not in best or key < best[r][0]:
            best[r] = (key, b)

    def net_of(b):
        r = rep(b)
        return best[r][1] if r in best else b

    for b, kind in port_bits:
        d.add_port(b, kind, net=net_of(b))
    for cell, inst, conns, line in m.cells:
        if cell in mods:
            raise ParseError("%s:%d: hierarchical instance %s of module %s: flatten the netlist first"
                             % (fname, line, inst, cell))
        if inst in d.insts:
            raise ParseError("%s:%d: duplicate instance %s" % (fname, line, inst))
        d.add_inst(inst, cell)
        for pin, bits in conns:
            rb = _resolve(m, bits, fname, line)
            if len(rb) > 1:
                raise ParseError("%s:%d: %s.%s: multi-bit connection to a cell pin" % (fname, line, inst, pin))
            for b in rb:
                d.connect(inst, pin, net_of(b))
    return d


def read_verilog(paths, top=None):
    """Read one or more files; returns the Design of `top` (default: the only uninstantiated module)."""
    mods = {}
    for p in [paths] if isinstance(paths, str) else paths:
        with open(p, errors="replace") as f:
            for name, m in _Parser(f.read(), p).modules().items():
                if name in mods:
                    raise ParseError("%s: module %s defined twice" % (p, name))
                m.fname = p
                mods[name] = m
    return _pick(mods, top)


def parse_verilog(text, top=None, fname="<string>"):
    mods = _Parser(text, fname).modules()
    for m in mods.values():
        m.fname = fname
    return _pick(mods, top)


def _pick(mods, top):
    if top is None:
        used = {c for m in mods.values() for c, _, _, _ in m.cells}
        tops = [n for n in mods if n not in used]
        if len(tops) != 1:
            raise ParseError("cannot pick the top module from %s: pass top=" % sorted(tops or mods))
        top = tops[0]
    if top not in mods:
        raise ParseError("module %s not found" % top)
    return _build(mods[top], mods[top].fname, mods)


# ============================================================================= write
def _vname(name, buses):
    """Internal bit-level name -> Verilog reference."""
    m = _BIT.match(name)
    if m and "\\" not in name and m.group(1) in buses:
        return name
    if _ID.match(name) and name not in _KEYWORDS:
        return name
    if name in CONSTS:
        return name
    return "\\" + _unescape(name) + " "


def _groups(names, buses):
    """Group bit names into (base, msb, lsb | None, [bits]) declarations, in first-seen order."""
    out, seen = [], {}
    for n in names:
        m = _BIT.match(n)
        if m and "\\" not in n and _ID.match(m.group(1)):
            base, idx = m.group(1), int(m.group(2))
            if base in seen:
                seen[base][3].append(idx)
                continue
            g = [base, None, None, [idx]]
            seen[base] = g
            out.append(g)
        else:
            out.append([n, None, None, None])
    res = []
    for base, _, _, idxs in out:
        if idxs is None:
            res.append((base, None, None))
        elif base in buses:
            res.append((base,) + tuple(buses[base]))
        else:
            res.append((base, max(idxs), min(idxs)))
    return res


def format_verilog(d):
    bus_bases = {b for b, msb, lsb in _groups(list(d.ports) + list(d.nets), d.buses) if msb is not None}
    ports = _groups(list(d.ports), d.buses)
    L = ["// %s: structural netlist written by Digital_Layout_Auto" % d.name,
         "module %s (%s);" % (_vname(d.name, ()), ", ".join(_vname(b, ()) for b, _, _ in ports))]
    first = {}
    for p in d.ports.values():
        m = _BIT.match(p.name)
        base = m.group(1) if m and m.group(1) in bus_bases else p.name
        first.setdefault(base, p.direction.lower())
    for base, msb, lsb in ports:
        rng = "" if msb is None else "[%d:%d] " % (msb, lsb)
        L.append("  %s %s%s;" % (first[base], rng, _vname(base, ())))
    port_names = set(d.ports)
    wires = [n for n in d.nets if n not in port_names and n not in CONSTS]
    for base, msb, lsb in _groups(wires, d.buses):
        if base in {b for b, _, _ in ports}:
            continue
        rng = "" if msb is None else "[%d:%d] " % (msb, lsb)
        L.append("  wire %s%s;" % (rng, _vname(base, ())))
    for p in d.ports.values():
        if (p.net or p.name) != p.name:
            if p.direction == "INPUT":
                raise ValueError("input port %s is aliased to net %s" % (p.name, p.net))
            L.append("  assign %s = %s;" % (_vname(p.name, bus_bases), _vname(p.net, bus_bases)))
    pins = d.inst_pins()
    for inst in d.insts.values():
        conns = pins.get(inst.name, {})
        body = ", ".join(".%s(%s)" % (p, _vname(conns[p], bus_bases)) for p in sorted(conns))
        L.append("  %s %s (%s);" % (inst.master, _vname(inst.name, ()), body))
    L.append("endmodule")
    return "\n".join(L) + "\n"


def write_verilog(d, path):
    with open(path, "w") as f:
        f.write(format_verilog(d))
    return path
