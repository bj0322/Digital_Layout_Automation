"""Liberty (.lib) timing libraries: generic parser and writer, NLDM semantics, functions.

    from dla.db.liberty import read_liberty
    lib = read_liberty("sg13g2_stdcell_typ_1p20V_25C.lib")
    c = lib.cells["sg13g2_nand2_1"]
    c.area, c.pins["Y"].function.truth(["A", "B"])        # [1, 1, 1, 0]
    arc = c.pins["Y"].arc("A")                            # timing arc A -> Y
    arc.tables["cell_rise"].delay(slew=0.1, load=0.02)    # ns
    c.pins["A"].capacitance                               # pF

Every time is converted to ns and every capacitance to pF on read, whatever units the
library declares. The parser builds a generic, order-preserving tree of ``Group``s
(written back by :func:`format_liberty`); the semantic layer (:class:`LibertyLib`)
reads NLDM timing, pin data, ``ff`` / ``latch`` groups and Boolean functions. Content
it does not model (statetable, bus pins, ...) is listed in ``LibCell.unsupported``.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .lex import ParseError


# ============================================================================= generic tree
class Group:
    """`type (args) { items }`; items keep file order: ("attr", name, value) |
    ("complex", name, [values]) | ("group", Group)."""

    def __init__(self, type_, args=None, line=0):
        self.type = type_
        self.args = list(args or [])
        self.items = []
        self.line = line

    @property
    def name(self):
        return self.args[0] if self.args else ""

    def attr(self, name, default=None):
        for it in self.items:
            if it[0] == "attr" and it[1] == name:
                return it[2]
        return default

    def complex(self, name):
        for it in self.items:
            if it[0] == "complex" and it[1] == name:
                return it[2]
        return None

    def groups(self, type_=None):
        return [it[1] for it in self.items if it[0] == "group" and (type_ is None or it[1].type == type_)]

    def to_data(self):
        """Plain nested data, for equality checks."""
        return (self.type, tuple(self.args),
                tuple((it[0], it[1].to_data()) if it[0] == "group" else
                      (it[0], it[1], tuple(it[2]) if it[0] == "complex" else it[2]) for it in self.items))

    def __repr__(self):
        return "Group(%s %s, %d items)" % (self.type, self.args, len(self.items))


_TOK = re.compile(r'''
    (?P<ws>[ \t\r\f]+|\\\r?\n) |
    (?P<nl>\n) |
    (?P<comment>/\*.*?\*/|//[^\n]*) |
    (?P<str>"(?:[^"\\\n]|\\.|\\\r?\n)*") |
    (?P<punct>[{}();:,]) |
    (?P<word>[^\s{}();:,"]+)
''', re.S | re.X)


def _tokenize(text, fname):
    """[(kind, value, line)]  kind: 'str' | 'word' | punct char."""
    out = []
    line = 1
    pos, n = 0, len(text)
    while pos < n:
        m = _TOK.match(text, pos)
        if not m:
            raise ParseError("%s:%d: unexpected character %r" % (fname, line, text[pos]))
        k = m.lastgroup
        v = m.group(k)
        if k == "str":
            out.append(("str", re.sub(r"\\\r?\n", "", v[1:-1]), line))
        elif k == "punct":
            out.append((v, v, line))
        elif k == "word":
            out.append(("word", v, line))
        line += v.count("\n")
        pos = m.end()
    return out


class _Parser:
    def __init__(self, text, fname):
        self.t = _tokenize(text, fname)
        self.i = 0
        self.fname = fname

    def err(self, msg, line=None):
        if line is None:
            line = self.t[min(self.i, len(self.t) - 1)][2] if self.t else 0
        return ParseError("%s:%d: %s" % (self.fname, line, msg))

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else (None, None, self.t[-1][2] if self.t else 0)

    def next(self):
        if self.i >= len(self.t):
            raise self.err("unexpected end of file")
        self.i += 1
        return self.t[self.i - 1]

    def expect(self, kind):
        tok = self.next()
        if tok[0] != kind:
            self.i -= 1
            raise self.err("expected %r, got %r" % (kind, tok[1]))
        return tok

    def top(self):
        groups = []
        while self.i < len(self.t):
            item = self.statement()
            if item[0] != "group":
                raise self.err("expected a library group", item[-1] if isinstance(item[-1], int) else None)
            groups.append(item[1])
        return groups

    def statement(self):
        kind, name, line = self.next()
        if kind not in ("word", "str"):
            raise self.err("unexpected %r" % name, line)
        nxt = self.peek()[0]
        if nxt == ":":                                    # simple attribute
            self.next()
            vals = []
            while True:
                k, v, ln = self.peek()
                if k in (";", "}", None) or (ln != line and vals):
                    break
                self.next()
                if k not in ("word", "str"):
                    raise self.err("unexpected %r in attribute %s" % (v, name), ln)
                vals.append(v)
            if self.peek()[0] == ";":
                self.next()
            if not vals:
                raise self.err("attribute %s has no value" % name, line)
            return ("attr", name, " ".join(vals))
        if nxt == "(":
            self.next()
            args = []
            while self.peek()[0] != ")":
                k, v, ln = self.next()
                if k == ",":
                    continue
                if k not in ("word", "str"):
                    raise self.err("unexpected %r in %s(...)" % (v, name), ln)
                args.append(v)
            self.next()
            if self.peek()[0] == "{":
                self.next()
                g = Group(name, args, line)
                while self.peek()[0] != "}":
                    if self.peek()[0] is None:
                        raise self.err("group %s(%s) opened on line %d is not closed"
                                       % (name, ",".join(args), line))
                    g.items.append(self.statement())
                self.next()
                return ("group", g)
            if self.peek()[0] == ";":
                self.next()
            return ("complex", name, args)
        raise self.err("expected ':' or '(' after %s" % name, line)


def parse_liberty_tree(text, fname="<string>"):
    groups = _Parser(text, fname).top()
    if len(groups) != 1 or groups[0].type != "library":
        raise ParseError("%s: expected one library group" % fname)
    return groups[0]


_PLAIN = re.compile(r"-?[0-9.]+([eE][-+]?\d+)?$|[A-Za-z_][A-Za-z0-9_.]*$")


def _fmt_val(v):
    return v if _PLAIN.match(v) else '"%s"' % v


def format_liberty(g, indent=0):
    """Write a Group tree back as Liberty text."""
    pad = "  " * indent
    L = ["%s%s (%s) {" % (pad, g.type, ", ".join(_fmt_val(a) for a in g.args))]
    for it in g.items:
        if it[0] == "attr":
            L.append("%s  %s : %s;" % (pad, it[1], _fmt_val(it[2])))
        elif it[0] == "complex":
            vals = it[2]
            if len(vals) > 1 and any("," in v for v in vals):
                L.append("%s  %s ( \\" % (pad, it[1]))
                L.append(", \\\n".join('%s    "%s"' % (pad, v) for v in vals) + " \\")
                L.append("%s  );" % pad)
            else:
                L.append("%s  %s (%s);" % (pad, it[1], ", ".join(_fmt_val(v) for v in vals)))
        else:
            L.append(format_liberty(it[1], indent + 1))
    L.append(pad + "}")
    return "\n".join(L) + ("\n" if indent == 0 else "")


# ============================================================================= functions
class Expr:
    """Boolean function of a Liberty pin. Nodes: ('var', n) ('const', 0|1) ('not', e)
    ('and', [e..]) ('or', [e..]) ('xor', [e..])."""

    def __init__(self, node, text=""):
        self.node = node
        self.text = text

    def vars(self):
        out = []

        def walk(n):
            if n[0] == "var":
                if n[1] not in out:
                    out.append(n[1])
            elif n[0] == "not":
                walk(n[1])
            elif n[0] in ("and", "or", "xor"):
                for c in n[1]:
                    walk(c)
        walk(self.node)
        return out

    def eval(self, env):
        def ev(n):
            k = n[0]
            if k == "var":
                return env[n[1]]
            if k == "const":
                return n[1]
            if k == "not":
                return 1 - ev(n[1])
            vals = [ev(c) for c in n[1]]
            if k == "and":
                return int(all(vals))
            if k == "or":
                return int(any(vals))
            r = 0
            for v in vals:
                r ^= v
            return r
        return ev(self.node)

    def truth(self, inputs=None):
        """Outputs for every input combination; inputs[0] is the most significant bit."""
        inputs = list(inputs) if inputs is not None else sorted(self.vars())
        n = len(inputs)
        return [self.eval({p: (k >> (n - 1 - i)) & 1 for i, p in enumerate(inputs)}) for k in range(1 << n)]

    def unateness(self, var, inputs=None, when=None):
        """'positive_unate' | 'negative_unate' | 'non_unate' | None (independent).

        when: optional Expr; only input states where it is 1 count (state-dependent arcs)."""
        inputs = list(inputs) if inputs is not None else self.vars()
        if when is not None:
            inputs += [v for v in when.vars() if v not in inputs]
        others = [p for p in inputs if p != var]
        up = down = False
        for k in range(1 << len(others)):
            env = {p: (k >> i) & 1 for i, p in enumerate(others)}
            if when is not None and not when.eval(env):
                continue
            env[var] = 0
            f0 = self.eval(env)
            env[var] = 1
            f1 = self.eval(env)
            up |= f1 > f0
            down |= f1 < f0
        if up and down:
            return "non_unate"
        return "positive_unate" if up else "negative_unate" if down else None

    def __repr__(self):
        return "Expr(%r)" % self.text


_FTOK = re.compile(r"\s*(?:([A-Za-z_][A-Za-z0-9_\[\]\.]*)|([01])\b|([!'*&+|^()]))")


def parse_function(text):
    toks = []
    pos = 0
    s = text.strip()
    while pos < len(s):
        m = _FTOK.match(s, pos)
        if not m or m.end() == pos:
            if s[pos:].strip() == "":
                break
            raise ParseError("bad function %r at %r" % (text, s[pos:]))
        if m.group(1):
            toks.append(("id", m.group(1)))
        elif m.group(2):
            toks.append(("const", int(m.group(2))))
        else:
            toks.append((m.group(3), m.group(3)))
        pos = m.end()
    i = [0]

    def peek():
        return toks[i[0]][0] if i[0] < len(toks) else None

    def take():
        i[0] += 1
        return toks[i[0] - 1]

    # precedence (low -> high): OR (+ |), AND (* & juxtaposition), XOR (^), NOT (! ')
    def p_or():
        items = [p_and()]
        while peek() in ("+", "|"):
            take()
            items.append(p_and())
        return items[0] if len(items) == 1 else ("or", items)

    def p_and():
        items = [p_xor()]
        while peek() in ("*", "&", "id", "const", "(", "!"):
            if peek() in ("*", "&"):
                take()
            items.append(p_xor())
        return items[0] if len(items) == 1 else ("and", items)

    def p_xor():
        items = [p_unary()]
        while peek() == "^":
            take()
            items.append(p_unary())
        return items[0] if len(items) == 1 else ("xor", items)

    def p_unary():
        if peek() == "!":
            take()
            return ("not", p_unary())
        k = peek()
        if k == "id":
            e = ("var", take()[1])
        elif k == "const":
            e = ("const", take()[1])
        elif k == "(":
            take()
            e = p_or()
            if peek() != ")":
                raise ParseError("bad function %r: missing ')'" % text)
            take()
        else:
            raise ParseError("bad function %r" % text)
        while peek() == "'":
            take()
            e = ("not", e)
        return e

    node = p_or()
    if i[0] != len(toks):
        raise ParseError("bad function %r" % text)
    return Expr(node, text)


# ============================================================================= semantics
_TIME = {"s": 1e9, "ms": 1e6, "us": 1e3, "ns": 1.0, "ps": 1e-3, "fs": 1e-6}
_CAP = {"f": 1e12, "mf": 1e9, "uf": 1e6, "nf": 1e3, "pf": 1.0, "ff": 1e-3}
_TIME_VARS = {"input_net_transition", "input_transition_time", "related_pin_transition",
              "constrained_pin_transition", "related_out_pin_transition"}
_CAP_VARS = {"total_output_net_capacitance", "related_out_total_output_net_capacitance",
             "equal_or_opposite_output_net_capacitance"}
TIMING_TABLES = ("cell_rise", "cell_fall", "rise_transition", "fall_transition",
                 "rise_constraint", "fall_constraint")


def _floats(vals):
    out = []
    for v in vals:
        out += [float(x) for x in re.split(r"[,\s]+", v.strip()) if x]
    return out


def _interp(xs, x):
    """(i, t): x lies at xs[i] + t * (xs[i+1] - xs[i]); outer intervals extrapolate."""
    if len(xs) == 1:
        return 0, 0.0
    i = 0
    while i < len(xs) - 2 and x > xs[i + 1]:
        i += 1
    return i, (x - xs[i]) / (xs[i + 1] - xs[i])


@dataclass
class Table:
    vars: List[str]                          # template variable names, in index order
    index: List[List[float]]                 # one list per variable (ns / pF)
    values: List[List[float]]                # values[i1][i2] (rows = index_1); 1-D: [[v..]]

    def lookup(self, **kw):
        """Value at the given variables (by template name); missing ones -> first index."""
        if not self.vars:
            return self.values[0][0]
        x = [kw.get(v, self.index[k][0]) for k, v in enumerate(self.vars)]
        if len(self.vars) == 1:
            row = self.values[0] if len(self.values) == 1 else [r[0] for r in self.values]
            i, t = _interp(self.index[0], x[0])
            if len(row) == 1:
                return row[0]
            return row[i] + t * (row[i + 1] - row[i])
        i, t = _interp(self.index[0], x[0])
        j, u = _interp(self.index[1], x[1])
        V = self.values
        j1 = min(j + 1, len(V[0]) - 1)
        i1 = min(i + 1, len(V) - 1)
        a = V[i][j] + u * (V[i][j1] - V[i][j])
        b = V[i1][j] + u * (V[i1][j1] - V[i1][j])
        return a + t * (b - a)

    def delay(self, slew, load):
        """Delay / transition table: input slew (ns) and output load (pF)."""
        return self.lookup(input_net_transition=slew, total_output_net_capacitance=load,
                           input_transition_time=slew)

    def constraint(self, constrained, related):
        """Setup / hold / recovery / removal / pulse width table (ns)."""
        return self.lookup(constrained_pin_transition=constrained, related_pin_transition=related)


@dataclass
class TimingArc:
    related: str
    type: str = "combinational"
    sense: Optional[str] = None
    when: Optional[str] = None
    tables: Dict[str, Table] = field(default_factory=dict)

    def is_check(self):
        return self.type.startswith(("setup", "hold", "recovery", "removal", "non_seq", "skew",
                                     "min_pulse", "minimum_period", "nochange"))


@dataclass
class LibPin:
    name: str
    direction: str = "input"
    capacitance: float = 0.0
    rise_cap: Optional[float] = None
    fall_cap: Optional[float] = None
    max_cap: Optional[float] = None
    min_cap: Optional[float] = None
    max_transition: Optional[float] = None
    function: Optional[Expr] = None
    three_state: Optional[Expr] = None
    clock: bool = False
    arcs: List[TimingArc] = field(default_factory=list)

    def arc(self, related, type_="combinational"):
        """The arc from `related`; the unconditional one if there are state-dependent ones."""
        found = [a for a in self.arcs if a.related == related and a.type == type_]
        found.sort(key=lambda a: a.when is not None)
        return found[0] if found else None


@dataclass
class Seq:
    kind: str                                # ff | latch
    state: List[str]                         # e.g. ["IQ", "IQN"]
    clocked_on: Optional[Expr] = None        # ff
    next_state: Optional[Expr] = None        # ff
    enable: Optional[Expr] = None            # latch
    data_in: Optional[Expr] = None           # latch
    clear: Optional[Expr] = None
    preset: Optional[Expr] = None
    clear_preset_var1: Optional[str] = None
    clear_preset_var2: Optional[str] = None


@dataclass
class LibCell:
    name: str
    area: float = 0.0
    footprint: Optional[str] = None
    dont_use: bool = False
    leakage: Optional[float] = None
    pins: Dict[str, LibPin] = field(default_factory=dict)
    seq: Optional[Seq] = None
    unsupported: List[str] = field(default_factory=list)
    attrs: Dict[str, str] = field(default_factory=dict)

    def inputs(self):
        return [p for p in self.pins.values() if p.direction == "input"]

    def outputs(self):
        return [p for p in self.pins.values() if p.direction in ("output", "inout")]

    def is_sequential(self):
        return self.seq is not None


class LibertyLib:
    def __init__(self, root, path=None):
        self.root = root
        self.path = path
        self.name = root.name
        a = root.attr
        self.time_scale = self._unit(a("time_unit", "1ns"), _TIME, "time_unit")
        cu = root.complex("capacitive_load_unit")
        if cu:
            self.cap_scale = float(cu[0]) * _CAP[cu[1].lower()]
        else:
            self.cap_scale = 1.0
        self.voltage = _num(a("nom_voltage"))
        self.temperature = _num(a("nom_temperature"))
        self.process = _num(a("nom_process"))
        self.thresholds = {k: _num(a(k)) for k in (
            "input_threshold_pct_rise", "input_threshold_pct_fall",
            "output_threshold_pct_rise", "output_threshold_pct_fall",
            "slew_lower_threshold_pct_rise", "slew_lower_threshold_pct_fall",
            "slew_upper_threshold_pct_rise", "slew_upper_threshold_pct_fall")}
        self.slew_derate = _num(a("slew_derate_from_library")) or 1.0
        self.defaults = {it[1]: it[2] for it in root.items if it[0] == "attr" and it[1].startswith("default_")}
        self.default_max_transition = self._t(_num(a("default_max_transition")))
        self.default_max_cap = self._c(_num(a("default_max_capacitance")))
        self.templates = {}
        for g in root.groups():
            if g.type.endswith("_template") or g.type == "power_lut_template":
                self.templates[g.name] = g
        self.cells: Dict[str, LibCell] = {}
        for g in root.groups("cell"):
            self.cells[g.name] = self._cell(g)

    @staticmethod
    def _unit(text, table, what):
        m = re.match(r"\s*([0-9.]+)\s*([a-zA-Z]+)\s*$", text)
        if not m or m.group(2).lower() not in table:
            raise ParseError("unsupported %s %r" % (what, text))
        return float(m.group(1)) * table[m.group(2).lower()]

    def _t(self, v):
        return None if v is None else v * self.time_scale

    def _c(self, v):
        return None if v is None else v * self.cap_scale

    def _cell(self, g):
        c = LibCell(g.name)
        c.area = _num(g.attr("area")) or 0.0
        c.footprint = g.attr("cell_footprint")
        c.dont_use = (g.attr("dont_use", "false").lower() == "true")
        c.leakage = _num(g.attr("cell_leakage_power"))
        for it in g.items:
            if it[0] == "attr" and it[1] not in ("area", "cell_footprint", "dont_use", "cell_leakage_power"):
                c.attrs[it[1]] = it[2]
        for sg in g.groups():
            t = sg.type
            if t == "pin":
                for name in sg.args:                  # pin (A, B) { ... } declares several pins
                    c.pins[name] = self._pin(sg, name, c)
            elif t in ("ff", "latch"):
                if c.seq is not None:
                    c.unsupported.append("more than one ff/latch group")
                c.seq = self._seq(sg)
            elif t in ("statetable", "bus", "bundle", "ff_bank", "latch_bank", "pg_pin"):
                if t != "pg_pin":
                    c.unsupported.append(t)
            elif t == "test_cell":
                c.attrs["test_cell"] = "true"
        return c

    def _pin(self, g, name, cell):
        p = LibPin(name)
        a = g.attr
        p.direction = (a("direction", "input") or "input").lower()
        p.capacitance = self._c(_num(a("capacitance"))) or 0.0
        p.rise_cap = self._c(_num(a("rise_capacitance")))
        p.fall_cap = self._c(_num(a("fall_capacitance")))
        p.max_cap = self._c(_num(a("max_capacitance")))
        p.min_cap = self._c(_num(a("min_capacitance")))
        p.max_transition = self._t(_num(a("max_transition")))
        p.clock = (a("clock", "false").lower() == "true")
        try:
            if a("function"):
                p.function = parse_function(a("function"))
            if a("three_state"):
                p.three_state = parse_function(a("three_state"))
        except ParseError as e:
            raise ParseError("%s:%d: cell %s pin %s: %s" % (self.path or "<lib>", g.line, cell.name, name, e))
        for tg in g.groups("timing"):
            rel = (tg.attr("related_pin") or "").split()
            for r in rel or [""]:
                arc = TimingArc(r, tg.attr("timing_type", "combinational"), tg.attr("timing_sense"),
                                tg.attr("when"))
                for kind in TIMING_TABLES:
                    for tab in tg.groups(kind):
                        arc.tables[kind] = self._table(tab)
                p.arcs.append(arc)
        return p

    def _seq(self, g):
        s = Seq(g.type, list(g.args))
        for key in ("clocked_on", "next_state", "enable", "data_in", "clear", "preset"):
            if g.attr(key):
                setattr(s, key, parse_function(g.attr(key)))
        s.clear_preset_var1 = g.attr("clear_preset_var1")
        s.clear_preset_var2 = g.attr("clear_preset_var2")
        return s

    def _table(self, g):
        tname = g.name
        tmpl = self.templates.get(tname)
        vals = g.complex("values")
        if vals is None:
            raise ParseError("%s:%d: table without values" % (self.path or "<lib>", g.line))
        if tname == "scalar" or tmpl is None and not g.complex("index_1"):
            return Table([], [], [[_floats(vals)[0] * self.time_scale]])
        vars_, index = [], []
        for k in (1, 2, 3):
            var = tmpl.attr("variable_%d" % k) if tmpl is not None else None
            idx = g.complex("index_%d" % k) or (tmpl.complex("index_%d" % k) if tmpl is not None else None)
            if var is None and idx is None:
                break
            if k == 3:
                raise ParseError("%s:%d: 3-D tables are not supported" % (self.path or "<lib>", g.line))
            scale = self.time_scale if var in _TIME_VARS else self.cap_scale if var in _CAP_VARS else 1.0
            vars_.append(var)
            index.append([v * scale for v in _floats(idx)])
        rows = [[v * self.time_scale for v in _floats([r])] for r in vals]
        if len(vars_) == 2 and (len(rows) != len(index[0]) or any(len(r) != len(index[1]) for r in rows)):
            raise ParseError("%s:%d: table size does not match its indices" % (self.path or "<lib>", g.line))
        return Table(vars_, index, rows)


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ============================================================================= entry points
def parse_liberty(text, fname="<string>"):
    return LibertyLib(parse_liberty_tree(text, fname), fname)


def read_liberty(path):
    with open(path, errors="replace") as f:
        return parse_liberty(f.read(), path)
