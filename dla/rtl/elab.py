"""Elaboration: Verilog AST -> flat bit-level AIG with flip-flops.

    from dla.rtl import elaborate
    r = elaborate(["rtl/gcd.v"], top="gcd", params={"W": 16})
    r.aig                 # dla.synth.aig.AIG: PIs / POs per port bit ("a[3]"), FFs ("u1.state[2]")
    r.ports               # [(name, direction, msb, lsb | None for scalars)]
    r.warnings

What happens here:
  * parameters are evaluated (overrides by name), generate loops / ifs unrolled,
    module instances inlined with hierarchical names ("u1.cnt[3]");
  * every expression is evaluated symbolically to a vector of AIG literals with the
    Verilog width and signedness rules (context-determined operands, self-determined
    concatenations / comparisons / shift amounts);
  * `always @*` blocks are executed symbolically (if / case become muxes); a variable
    that is not assigned on every path is a latch and stops elaboration;
  * edge-triggered blocks give one FF per assigned bit; an outer
    `if (rst) ... else ...` on an extra edge of the sensitivity list becomes an
    asynchronous reset / set with a constant value;
  * wires are resolved lazily, so declaration order does not matter; combinational
    loops and multiple drivers are errors.
Unknown (x / z) values in expressions are taken as 0; x / z / ? in casez / casex
labels are don't-cares.
"""
import math
import sys
import threading

from ..db.lex import ParseError
from ..synth.aig import AIG
from ..synth.words import Words
from .parser import N, parse_files, parse_text

PARTIAL = -1                 # comb block: bit assigned on some paths only (latch)


class ElabError(ParseError):
    pass


def _err(node, msg):
    if node is not None and getattr(node, "file", None):
        return ElabError("%s:%d: %s" % (node.file, node.line, msg))
    return ElabError(msg)


class NotConst(Exception):
    pass


# ============================================================================= objects
class Sig:
    __slots__ = ("name", "width", "signed", "msb", "lsb", "kind", "dir", "src", "val", "busy",
                 "scalar", "decl", "warned")

    def __init__(self, name, msb, lsb, signed, kind, decl, scalar):
        self.name = name
        self.msb, self.lsb = msb, lsb
        self.width = abs(msb - lsb) + 1
        self.signed = signed
        self.kind = kind                  # wire | reg | integer
        self.dir = None
        self.src = [None] * self.width
        self.val = [None] * self.width
        self.busy = [False] * self.width
        self.scalar = scalar
        self.decl = decl
        self.warned = False

    def pos(self, idx):
        """Declared index -> bit position (0 = LSB), None if out of range."""
        p = idx - self.lsb if self.msb >= self.lsb else self.lsb - idx
        return p if 0 <= p < self.width else None

    def bitname(self, p):
        if self.scalar:
            return self.name
        idx = self.lsb + p if self.msb >= self.lsb else self.lsb - p
        return "%s[%d]" % (self.name, idx)


class Const:
    __slots__ = ("bits", "signed")

    def __init__(self, bits, signed):
        self.bits, self.signed = bits, signed


class Func:
    __slots__ = ("node", "scope")

    def __init__(self, node, scope):
        self.node, self.scope = node, scope


class Scope:
    def __init__(self, parent, prefix, module=None):
        self.parent = parent
        self.prefix = prefix
        self.names = {}
        self.module = module if module is not None else (parent.module if parent else None)
        self.gen_count = 0

    def lookup(self, name):
        s = self
        while s is not None:
            if name in s.names:
                return s.names[name]
            s = s.parent
        return None

    def mod_scope(self):
        s = self
        while s.parent is not None and s.parent.module is s.module:
            s = s.parent
        return s


class Driver:
    """Continuous driver: lhs bits [(sig, pos)] <- rhs evaluated in rhs_scope."""
    __slots__ = ("targets", "scope", "rhs", "val", "busy", "node")

    def __init__(self, targets, scope, rhs, node):
        self.targets, self.scope, self.rhs, self.node = targets, scope, rhs, node
        self.val = None
        self.busy = False


class Block:
    __slots__ = ("node", "scope", "kind", "targets", "env", "busy", "ffs")

    def __init__(self, node, scope, kind, targets):
        self.node, self.scope, self.kind, self.targets = node, scope, kind, targets
        self.env = None
        self.busy = False
        self.ffs = {}                     # (sig name, pos) -> FF name


class Result:
    def __init__(self, aig, ports, warnings, top):
        self.aig, self.ports, self.warnings, self.top = aig, ports, warnings, top


# ============================================================================= elaborator
class Elaborator:
    MAX_LOOP = 100000

    def __init__(self, mods):
        self.mods = mods
        self.g = AIG()
        self.W = Words(self.g)
        self.warnings = []
        self.sigs = {}
        self.blocks = []
        self.depth = 0
        self.const_mode = 0

    # ------------------------------------------------------------------ top level
    def run(self, top, params=None):
        if top not in self.mods:
            raise ElabError("top module %s not found" % top)
        m = self.mods[top]
        scope = self.instantiate(m, "", params or {}, None, None)
        for pname in m[2]:
            s = scope.names.get(pname)
            if isinstance(s, Sig) and s.dir == "input":
                for p in range(s.width):
                    self.set_src(s, p, ("pi", self.g.pi(s.bitname(p))), m)
        ports = []
        for pname in m[2]:
            s = scope.names.get(pname)
            if not isinstance(s, Sig) or s.dir is None:
                raise _err(m, "port %s has no direction" % pname)
            ports.append((pname, s.dir, None if s.scalar else s.msb, None if s.scalar else s.lsb))
        for b in self.blocks:
            if b.kind == "seq":
                self.eval_seq(b)
        for pname, d, _, _ in ports:
            s = scope.names[pname]
            if d == "output":
                for p in range(s.width):
                    self.g.po(s.bitname(p), self.resolve(s, p, s.decl))
        for b in self.blocks:                # report latches / loops even in unused logic
            if b.kind == "comb":
                self.eval_comb(b)
        return Result(self.g, ports, self.warnings, top)

    # ------------------------------------------------------------------ instantiation
    def instantiate(self, m, prefix, overrides, parent_scope, inst):
        self.depth += 1
        if self.depth > 64:
            raise _err(inst, "module hierarchy too deep (recursive instantiation?)")
        scope = Scope(None, prefix, module=m)
        # parameters (header first, then body), overrides by name
        params = list(m[1]) + [it for it in m[3] if it.k == "param"]
        names = {p[0] for p in params if not p[4]}
        for k in overrides:
            if k not in names:
                raise _err(inst if inst is not None else m, "module %s has no parameter %s" % (m[0], k))
        for p in params:
            if not p[4] and p[0] in overrides:
                val = overrides[p[0]]
                if isinstance(val, int):
                    val = Const(self.W.const(val & ((1 << 64) - 1), 64), val < 0)
                scope.names[p[0]] = self.param_value(p, scope, val)
            else:
                scope.names[p[0]] = self.param_value(p, scope, None)
        self.items(m[3], scope, top_items=True)
        self.depth -= 1
        return scope

    def param_value(self, p, scope, given):
        name, e, rng, signed, _ = p.a
        if given is None:
            sz = self.size(e, scope)
            bits = self.ev(e, sz[0], sz[1], scope, None)
            if not self.W.is_const(bits):
                raise _err(e, "parameter %s is not constant" % name)
            given = Const(bits, sz[1])
        if rng is not None:
            msb, lsb = self.const_int(rng[0], scope), self.const_int(rng[1], scope)
            w = abs(msb - lsb) + 1
            return Const(self.W.extend(given.bits, w, given.signed), signed)
        return Const(given.bits, given.signed or signed)

    def items(self, items, scope, top_items=False):
        # pass 1: declarations (and parameters in nested generate scopes)
        for it in items:
            if it.k == "decl":
                self.declare(it, scope)
            elif it.k == "param" and not top_items:
                scope.names[it[0]] = self.param_value(it, scope, None)
            elif it.k == "function":
                scope.names[it[0]] = Func(it, scope)
        # pass 2: behaviour
        for it in items:
            k = it.k
            if k in ("decl", "param", "function"):
                continue
            if k == "assign":
                self.add_driver(self.lv_targets(it[0], scope, implicit=True), scope, it[1], it)
            elif k == "always":
                self.add_always(it, scope)
            elif k == "inst":
                self.add_inst(it, scope)
            elif k == "genfor":
                self.genfor(it, scope)
            elif k == "genif":
                c = self.const_int(it[0], scope)
                for sub in (it[1] if c else it[2]):
                    self.items([sub], scope) if sub.k != "genblock" else self.genblock(sub, scope, None)
            elif k == "genblock":
                self.genblock(it, scope, None)
            else:
                raise _err(it, "unsupported item %s" % k)

    def genblock(self, it, scope, index):
        label = it[0]
        if label is None:
            scope.mod_scope().gen_count += 1
            label = "genblk%d" % scope.mod_scope().gen_count
        pre = scope.prefix + label + ("[%d]" % index if index is not None else "") + "."
        sub = Scope(scope, pre)
        self.items(it[1], sub)

    def genfor(self, it, scope):
        var, init, cond, step, label, items = it.a
        val = self.const_int(init, scope)
        if label is None:
            scope.mod_scope().gen_count += 1
            label = "genblk%d" % scope.mod_scope().gen_count
        n = 0
        while True:
            sub = Scope(scope, scope.prefix + "%s[%d]." % (label, val))
            sub.names[var] = Const(self.W.const(val & 0xFFFFFFFF, 32), True)
            if not self.const_int(cond, sub):
                break
            self.items(items, sub)
            val = self.const_int(step, sub)
            n += 1
            if n > self.MAX_LOOP:
                raise _err(it, "generate loop does not terminate")

    def declare(self, it, scope):
        kind, names, rng, signed, netkind = it.a
        if kind == "genvar":
            for name, _ in names:
                scope.names[name] = Sig(scope.prefix + name, 31, 0, True, "genvar", it, False)
            return
        if rng is not None:
            msb, lsb = self.const_int(rng[0], scope), self.const_int(rng[1], scope)
            scalar = False
        elif kind == "integer":
            msb, lsb, scalar = 31, 0, False
        else:
            msb, lsb, scalar = 0, 0, True
        for name, arr in names:
            if arr is not None:
                raise _err(it, "unsupported: memory (array) %s" % name)
            old = scope.names.get(name)
            if kind in ("input", "output", "inout"):
                if kind == "inout":
                    raise _err(it, "unsupported: inout port %s" % name)
                if isinstance(old, Sig):
                    if rng is not None and (old.msb, old.lsb) != (msb, lsb) and not old.scalar:
                        raise _err(it, "range of %s differs from its declaration" % name)
                    old.dir = kind
                    old.signed |= signed
                    if netkind == "reg":
                        old.kind = "reg"
                    continue
                s = self.new_sig(scope, name, msb, lsb, signed, netkind or "wire", it, scalar)
                s.dir = kind
                continue
            if isinstance(old, Sig):
                if old.dir is None:
                    raise _err(it, "%s declared twice" % name)
                if rng is not None and (old.msb, old.lsb) != (msb, lsb):
                    if not old.scalar:
                        raise _err(it, "range of %s differs from its port declaration" % name)
                    old2 = self.new_sig(scope, name, msb, lsb, signed or old.signed, kind, it, False, replace=True)
                    old2.dir = old.dir
                    continue
                old.kind = kind if kind != "wire" else old.kind
                old.signed |= signed
                continue
            self.new_sig(scope, name, msb, lsb, signed, kind, it, scalar)

    def new_sig(self, scope, name, msb, lsb, signed, kind, node, scalar, replace=False):
        full = scope.prefix + name
        if full in self.sigs and not replace:
            raise _err(node, "%s declared twice" % name)
        s = Sig(full, msb, lsb, signed, kind, node, scalar)
        self.sigs[full] = s
        scope.names[name] = s
        return s

    # ------------------------------------------------------------------ drivers
    def set_src(self, sig, p, src, node):
        if sig.src[p] is not None:
            raise _err(node, "multiple drivers for %s" % sig.bitname(p))
        sig.src[p] = src

    def add_driver(self, targets, scope, rhs, node):
        d = Driver(targets, scope, rhs, node)
        for k, (s, p) in enumerate(targets):
            if s.kind == "integer":
                raise _err(node, "continuous assignment to integer %s" % s.name)
            self.set_src(s, p, ("drv", d, k), node)
        return d

    def lv_targets(self, lv, scope, implicit=False, ex=None):
        """Constant lvalue -> [(sig, pos)] LSB first."""
        k = lv.k
        if k == "concat":
            out = []
            for item in reversed(lv[0]):
                out += self.lv_targets(item, scope, implicit, ex)
            return out
        base = lv
        while base.k in ("index", "range", "prange"):
            base = base[0]
        if base.k != "id":
            raise _err(lv, "unsupported assignment target")
        s = scope.lookup(base[0])
        if s is None:
            if not implicit or lv.k != "id":
                raise _err(lv, "undeclared identifier %s" % base[0])
            s = self.new_sig(scope.mod_scope(), base[0], 0, 0, False, "wire", lv, True)
        if not isinstance(s, Sig):
            raise _err(lv, "%s is not a variable" % base[0])
        if k == "id":
            return [(s, p) for p in range(s.width)]
        if lv[0].k != "id":
            raise _err(lv, "unsupported: nested selects on the left-hand side")
        if k == "index":
            p = s.pos(self.const_int(lv[1], scope, ex))
            if p is None:
                raise _err(lv, "index out of range of %s" % s.name)
            return [(s, p)]
        if k == "range":
            a, b = self.const_int(lv[1], scope, ex), self.const_int(lv[2], scope, ex)
            pa, pb = s.pos(a), s.pos(b)
            if pa is None or pb is None:
                raise _err(lv, "part select out of range of %s" % s.name)
            lo, hi = min(pa, pb), max(pa, pb)
            return [(s, p) for p in range(lo, hi + 1)]
        start, w = self.const_int(lv[1], scope, ex), self.const_int(lv[2], scope, ex)
        idxs = range(start, start + w) if lv[3] else range(start - w + 1, start + 1)
        ps = sorted(s.pos(i) for i in idxs if s.pos(i) is not None)
        if len(ps) != w:
            raise _err(lv, "part select out of range of %s" % s.name)
        return [(s, p) for p in ps]

    def add_inst(self, it, scope):
        modname, iname, pconns, conns = it.a
        if modname not in self.mods:
            raise _err(it, "unknown module %s (cells cannot be instantiated in RTL)" % modname)
        m = self.mods[modname]
        header = [p for p in m[1]] + [p for p in m[3] if p.k == "param" and not p[4]]
        overrides = {}
        for k, (pn, e) in enumerate(pconns):
            if pn is None:
                if k >= len(header):
                    raise _err(it, "too many parameters for %s" % modname)
                pn = header[k][0]
            sz = self.size(e, scope)
            bits = self.ev(e, sz[0], sz[1], scope, None)
            if not self.W.is_const(bits):
                raise _err(e, "parameter %s is not constant" % pn)
            overrides[pn] = Const(bits, sz[1])
        child = self.instantiate(m, scope.prefix + iname + ".", overrides, scope, it)
        ports = m[2]
        named = {}
        for k, (pn, e) in enumerate(conns):
            if pn is None:
                if k >= len(ports):
                    raise _err(it, "too many port connections for %s" % modname)
                pn = ports[k]
            if pn not in ports:
                raise _err(it, "module %s has no port %s" % (modname, pn))
            named[pn] = e
        for pn in ports:
            s = child.names[pn]
            e = named.get(pn)
            if s.dir == "input":
                if e is None:
                    self.warnings.append("%s:%d: input %s of %s unconnected (0)" % (it.file, it.line, pn, iname))
                    continue
                self.add_driver([(s, p) for p in range(s.width)], scope, e, it)
            elif e is not None:
                targets = self.lv_targets(e, scope, implicit=True)
                self.add_driver(targets, child, N("id", pn, at=it), it)

    def add_always(self, it, scope):
        sens, stmt = it.a
        edges = [e for e, _ in sens] if sens else []
        if any(edges) and not all(edges):
            raise _err(it, "unsupported: mixed edge and level sensitivity")
        kind = "seq" if any(edges) else "comb"
        targets = {}
        self.static_targets(stmt, scope, targets)
        b = Block(it, scope, kind, targets)
        for s, ps in targets.items():
            if s.kind == "integer":
                continue                     # block-local variable
            for p in ps:
                if kind == "comb":
                    self.set_src(s, p, ("comb", b), it)
                else:
                    if s.src[p] is not None:
                        raise _err(it, "multiple drivers for %s" % s.bitname(p))
                    name = s.bitname(p)
                    q = self.g.ff(name)
                    b.ffs[(s.name, p)] = name
                    self.set_src(s, p, ("ff", q), it)
        self.blocks.append(b)

    def static_targets(self, st, scope, out, local=frozenset()):
        """Signal bits a procedural statement may assign (named-block locals excluded)."""
        k = st.k
        if k in ("bassign", "nbassign"):
            for s, ps in self.lv_static(st[0], scope, local):
                out.setdefault(s, set()).update(ps)
        elif k == "block":
            if st[2]:
                local = local | {n for d in st[2] for n, _ in d[1]}
            for x in st[1]:
                self.static_targets(x, scope, out, local)
        elif k == "if":
            self.static_targets(st[1], scope, out, local)
            if st[2] is not None:
                self.static_targets(st[2], scope, out, local)
        elif k == "case":
            for _, x in st[2]:
                self.static_targets(x, scope, out, local)
        elif k == "for":
            self.static_targets(st[5], scope, out, local)
            if st[0] not in local:
                s = scope.lookup(st[0])
                if isinstance(s, Sig):
                    out.setdefault(s, set()).update(range(s.width))

    def lv_static(self, lv, scope, local=frozenset()):
        if lv.k == "concat":
            out = []
            for x in lv[0]:
                out += self.lv_static(x, scope, local)
            return out
        base = lv
        while base.k in ("index", "range", "prange"):
            base = base[0]
        if base.k == "id" and base[0] in local:
            return []
        s = scope.lookup(base[0]) if base.k == "id" else None
        if not isinstance(s, Sig):
            raise _err(lv, "assignment to undeclared or non-variable %s" % (base[0] if base.k == "id" else "?"))
        if s.kind == "wire" and s.dir is None:
            raise _err(lv, "procedural assignment to wire %s" % s.name)
        try:
            return [(s, [p for _, p in self.lv_targets(lv, scope)])]
        except (ElabError, NotConst):
            return [(s, list(range(s.width)))]

    # ------------------------------------------------------------------ resolution
    def resolve(self, s, p, node=None):
        v = s.val[p]
        if v is not None:
            return v
        if self.const_mode:
            raise NotConst()
        src = s.src[p]
        if src is None:
            if not s.warned:
                self.warnings.append("%s is not driven (taken as 0)" % s.bitname(p))
                s.warned = True
            s.val[p] = 0
            return 0
        if s.busy[p]:
            raise _err(node, "combinational loop through %s" % s.bitname(p))
        s.busy[p] = True
        kind = src[0]
        if kind in ("pi", "ff"):
            v = src[1]
        elif kind == "drv":
            v = self.eval_driver(src[1])[src[2]]
        elif kind == "comb":
            env = self.eval_comb(src[1])
            v = env.get(s.name, (None,) * s.width)[p]
            if v is None or v == PARTIAL:
                raise _err(src[1].node, "latch inferred for %s (not assigned on every path)" % s.bitname(p))
        s.busy[p] = False
        s.val[p] = v
        return v

    def eval_driver(self, d):
        if d.val is not None:
            return d.val
        if d.busy:
            raise _err(d.node, "combinational loop through %s" % d.targets[0][0].bitname(d.targets[0][1]))
        d.busy = True
        w = len(d.targets)
        sz = self.size(d.rhs, d.scope)
        d.val = self.ev(d.rhs, max(w, sz[0]), sz[1], d.scope, None)[:w]
        d.busy = False
        return d.val

    def eval_comb(self, b):
        if b.env is not None:
            return b.env
        if b.busy:
            raise _err(b.node, "combinational loop through this always block")
        b.busy = True
        ex = Exec(self, b.scope, "comb", b)
        ex.run(b.node[1])
        for s, ps in b.targets.items():
            if s.kind == "integer":
                continue
            vals = ex.env.get(s.name)
            for p in ps:
                v = vals[p] if vals is not None else None
                if v is None or v == PARTIAL:
                    raise _err(b.node, "latch inferred for %s (not assigned on every path)" % s.bitname(p))
        b.env = ex.env
        b.busy = False
        return b.env

    def eval_seq(self, b):
        it = b.node
        sens, stmt = it.a
        names = {}
        for edge, e in sens:
            if e.k != "id":
                raise _err(e, "unsupported: edge on an expression")
            names[e[0]] = edge
        # peel `if (async) ... else` chain
        chain = []
        body = stmt
        while True:
            s = body
            while s is not None and s.k == "block" and len(s[1]) == 1 and not s[2]:
                s = s[1][0]
            if s is not None and s.k == "if" and len(names) > 1:
                refs = self.refs(s[0])
                if refs and refs <= set(names) and len(refs) == 1:
                    chain.append((s[0], s[1], refs.pop()))
                    body = s[2]
                    continue
            break
        async_names = {r for _, _, r in chain}
        clocks = [n for n in names if n not in async_names]
        if len(clocks) != 1:
            raise _err(it, "cannot identify the clock among %s (async controls need an outer if)" % sorted(names))
        clk = clocks[0]
        clk_lit = self.ev(N("id", clk, at=it), 1, False, b.scope, None)[0]
        pi_name = self.g.name[clk_lit >> 1] if self.g.kind[clk_lit >> 1] == 1 else None
        if pi_name is None:
            raise _err(it, "clock %s must be a primary input (derived / gated clocks are not supported)" % clk)
        edge = names[clk]
        if clk_lit & 1:
            edge = "neg" if edge == "pos" else "pos"
        # async branches
        arst = {}                                  # (sig name, pos) -> (lit, value)
        any_async = 0
        for cond, branch, sname in chain:
            c = self.W.bool(self.ev_self(cond, b.scope, None))
            sig_lit = self.ev(N("id", sname, at=it), 1, False, b.scope, None)[0]
            want = "pos" if c == sig_lit else "neg" if c == sig_lit ^ 1 else None
            if want is None or want != names[sname]:
                raise _err(cond, "async condition on %s does not match its %sedge" % (sname, names[sname]))
            ex = Exec(self, b.scope, "seq", b)
            ex.run(branch)
            for key, vals in ex.final().items():
                sig = self.sigs[key]
                for p, v in enumerate(vals):
                    if v is None:
                        continue
                    if v not in (0, 1):
                        raise _err(branch, "async reset/set value of %s is not constant" % sig.bitname(p))
                    if (key, p) in arst:
                        raise _err(branch, "%s has more than one async control" % sig.bitname(p))
                    arst[(key, p)] = (c, v)
            any_async = self.g.OR(any_async, c)
        ex = Exec(self, b.scope, "seq", b)
        if body is not None:
            ex.run(body)
        final = ex.final()
        for (sname, p), ffname in b.ffs.items():
            sig = self.sigs[sname]
            q = 2 * self.g.ffs[ffname].q
            d = final.get(sname, [None] * sig.width)[p]
            d = q if d is None else d
            f = self.g.ffs[ffname]
            f.clk, f.edge = pi_name, edge
            if (sname, p) in arst:
                f.arst, f.arst_val = arst[(sname, p)]
                f.d = d
            else:
                f.d = self.g.MUX(any_async, q, d) if any_async else d

    def refs(self, e):
        out = set()

        def walk(n):
            if not isinstance(n, N):
                return
            if n.k == "id":
                out.add(n[0])
            for x in n.a:
                if isinstance(x, N):
                    walk(x)
                elif isinstance(x, list):
                    for y in x:
                        walk(y)
        walk(e)
        return out

    # ------------------------------------------------------------------ constants
    def const_int(self, e, scope, ex=None):
        self.const_mode += 1
        try:
            sz = self.size(e, scope, ex)
            bits = self.ev(e, max(sz[0], 1), sz[1], scope, ex)
        except NotConst:
            raise _err(e, "expression is not constant")
        finally:
            self.const_mode -= 1
        if not self.W.is_const(bits):
            raise _err(e, "expression is not constant")
        return self.W.to_int(bits, sz[1])

    # ------------------------------------------------------------------ sizing
    def size(self, e, scope, ex=None):
        """Self-determined (width, signed)."""
        k = e.k
        if k == "num":
            return (e[0] if e[0] is not None else 32, e[1])
        if k == "id":
            v = scope.lookup(e[0])
            if isinstance(v, Sig):
                return (v.width, v.signed)
            if isinstance(v, Const):
                return (len(v.bits), v.signed)
            raise _err(e, "undeclared identifier %s" % e[0])
        if k == "index":
            return (1, False)
        if k == "range":
            return (abs(self.const_int(e[1], scope, ex) - self.const_int(e[2], scope, ex)) + 1, False)
        if k == "prange":
            return (self.const_int(e[2], scope, ex), False)
        if k == "unop":
            if e[0] in ("+", "-", "~"):
                return self.size(e[1], scope, ex)
            return (1, False)
        if k == "binop":
            op = e[0]
            if op in ("==", "!=", "===", "!==", "<", "<=", ">", ">=", "&&", "||"):
                return (1, False)
            a = self.size(e[1], scope, ex)
            if op in ("<<", ">>", "<<<", ">>>", "**"):
                return (a[0], a[1] and (op != "**" or self.size(e[2], scope, ex)[1]))
            b = self.size(e[2], scope, ex)
            return (max(a[0], b[0]), a[1] and b[1])
        if k == "tern":
            a, b = self.size(e[1], scope, ex), self.size(e[2], scope, ex)
            return (max(a[0], b[0]), a[1] and b[1])
        if k == "concat":
            return (sum(self.size(x, scope, ex)[0] for x in e[0]), False)
        if k == "repl":
            return (self.const_int(e[0], scope, ex) * sum(self.size(x, scope, ex)[0] for x in e[1]), False)
        if k == "call":
            name = e[0]
            if name in ("$signed", "$unsigned"):
                return (self.size(e[1][0], scope, ex)[0], name == "$signed")
            if name == "$clog2":
                return (32, True)
            f = scope.lookup(name)
            if not isinstance(f, Func):
                raise _err(e, "unknown function %s" % name)
            fn = f.node
            if fn[1] is None:
                return (1, fn[2])
            return (abs(self.const_int(fn[1][0], f.scope) - self.const_int(fn[1][1], f.scope)) + 1, fn[2])
        raise _err(e, "unsupported expression %s" % k)

    def ev_self(self, e, scope, ex):
        w, s = self.size(e, scope, ex)
        return self.ev(e, w, s, scope, ex)

    # ------------------------------------------------------------------ evaluation
    def ev(self, e, w, s, scope, ex):
        """Evaluate e in a context of width w and signedness s -> w literals."""
        W, g = self.W, self.g
        k = e.k
        if k == "num":
            width = e[0] if e[0] is not None else 32
            if e[3] or e[4]:
                self.warn_x(e)
            bits = W.const(e[2] & ~(e[3] | e[4]), width)
            return W.extend(bits, w, s)
        if k == "id":
            v = scope.lookup(e[0])
            if isinstance(v, Const):
                return W.extend(v.bits, w, s)
            if not isinstance(v, Sig):
                raise _err(e, "undeclared identifier %s" % e[0])
            return W.extend(self.read(v, e, ex), w, s)
        if k in ("index", "range", "prange"):
            return W.extend(self.select(e, scope, ex), w, False)
        if k == "unop":
            op = e[0]
            if op == "+":
                return self.ev(e[1], w, s, scope, ex)
            if op == "-":
                return W.neg(self.ev(e[1], w, s, scope, ex))
            if op == "~":
                return W.inv(self.ev(e[1], w, s, scope, ex))
            a = self.ev_self(e[1], scope, ex)
            r = W.bool(a) ^ 1 if op == "!" else W.reduce(op, a)
            return W.extend([r], w, False)
        if k == "binop":
            return self.binop(e, w, s, scope, ex)
        if k == "tern":
            c = W.bool(self.ev_self(e[0], scope, ex))
            if c in (0, 1):
                return self.ev(e[1] if c else e[2], w, s, scope, ex)
            return W.mux(c, self.ev(e[1], w, s, scope, ex), self.ev(e[2], w, s, scope, ex))
        if k == "concat":
            bits = []
            for x in reversed(e[0]):
                bits += self.ev_self(x, scope, ex)
            return W.extend(bits, w, False)
        if k == "repl":
            n = self.const_int(e[0], scope, ex)
            if n < 0:
                raise _err(e, "negative replication")
            one = []
            for x in reversed(e[1]):
                one += self.ev_self(x, scope, ex)
            return W.extend(one * n, w, False)
        if k == "call":
            return self.call(e, w, s, scope, ex)
        raise _err(e, "unsupported expression %s" % k)

    def warn_x(self, e):
        msg = "%s:%d: x / z constant bits taken as 0" % (e.file, e.line)
        if msg not in self.warnings:
            self.warnings.append(msg)

    def read(self, sig, node, ex):
        if sig.kind == "genvar":
            raise _err(node, "genvar %s used outside its loop" % sig.name)
        if ex is not None:
            return ex.read(sig, node)
        if sig.kind == "integer" and all(x is None for x in sig.src):
            raise _err(node, "integer %s is only defined inside procedural blocks" % sig.name)
        return [self.resolve(sig, p, node) for p in range(sig.width)]

    def select(self, e, scope, ex):
        W = self.W
        base = e[0]
        if base.k != "id":
            raise _err(e, "unsupported: select of an expression")
        v = scope.lookup(base[0])
        if isinstance(v, Const):
            bits, msb, lsb = v.bits, len(v.bits) - 1, 0

            def pos(i):
                return i if 0 <= i < len(bits) else None
        elif isinstance(v, Sig):
            bits, msb, lsb = self.read(v, base, ex), v.msb, v.lsb
            pos = v.pos
        else:
            raise _err(e, "undeclared identifier %s" % base[0])
        n = len(bits)
        if e.k == "index":
            iw, isg = self.size(e[1], scope, ex)
            ib = self.ev(e[1], iw, isg, scope, ex)
            if W.is_const(ib):
                p = pos(W.to_int(ib, isg))
                return [bits[p] if p is not None else 0]
            return [self.dyn_bit(bits, ib, isg, msb, lsb)]
        if e.k == "range":
            a, b = self.const_int(e[1], scope, ex), self.const_int(e[2], scope, ex)
            lo, hi = sorted((a, b))
            out = []
            for i in range(lo, hi + 1) if msb >= lsb else range(hi, lo - 1, -1):
                p = pos(i)
                out.append(bits[p] if p is not None else 0)
            return out
        width = self.const_int(e[2], scope, ex)
        iw, isg = self.size(e[1], scope, ex)
        sb = self.ev(e[1], iw, isg, scope, ex)
        if W.is_const(sb):
            start = W.to_int(sb, isg)
            idxs = range(start, start + width) if e[3] else range(start - width + 1, start + 1)
            if msb < lsb:
                idxs = reversed(list(idxs))
            out = []
            for i in idxs:
                p = pos(i)
                out.append(bits[p] if p is not None else 0)
            return out
        # variable start: shift the vector right by the start position
        if msb < lsb:
            raise _err(e, "unsupported: variable part select of an ascending range")
        st = W.extend(sb, 32, isg)
        if not e[3]:
            st = W.sub(st, W.const(width - 1, 32))
        off = W.sub(st, W.const(lsb & 0xFFFFFFFF, 32))
        shifted = W.shift(bits + [0] * width, off, left=False)
        return shifted[:width]

    def dyn_bit(self, bits, ib, isg, msb, lsb):
        W = self.W
        iv = W.extend(ib, 32, isg)
        if msb >= lsb:
            off = W.sub(iv, W.const(lsb & 0xFFFFFFFF, 32))
        else:
            off = W.sub(W.const(lsb & 0xFFFFFFFF, 32), iv)
        return W.shift(bits, off, left=False)[0]

    def binop(self, e, w, s, scope, ex):
        W, g = self.W, self.g
        op = e[0]
        if op in ("&&", "||"):
            a = W.bool(self.ev_self(e[1], scope, ex))
            if (op == "&&" and a == 0) or (op == "||" and a == 1):
                return W.extend([a], w, False)
            b = W.bool(self.ev_self(e[2], scope, ex))
            return W.extend([g.AND(a, b) if op == "&&" else g.OR(a, b)], w, False)
        if op in ("==", "!=", "===", "!==", "<", "<=", ">", ">="):
            sa, sb = self.size(e[1], scope, ex), self.size(e[2], scope, ex)
            w2, s2 = max(sa[0], sb[0]), sa[1] and sb[1]
            a, b = self.ev(e[1], w2, s2, scope, ex), self.ev(e[2], w2, s2, scope, ex)
            return W.extend([W.compare(op, a, b, s2)], w, False)
        if op in ("<<", ">>", "<<<", ">>>"):
            a = self.ev(e[1], w, s, scope, ex)
            amt = self.ev_self(e[2], scope, ex)
            fill = a[-1] if (op == ">>>" and s) else 0
            left = op in ("<<", "<<<")
            if W.is_const(amt):
                return W.shift_const(a, W.to_int(amt), left, fill)
            return W.shift(a, amt, left, fill)
        a = self.ev(e[1], w, s, scope, ex)
        if op == "**":
            b = self.ev_self(e[2], scope, ex)
            if not (W.is_const(a) and W.is_const(b)):
                raise _err(e, "unsupported: ** with non-constant operands")
            x, y = W.to_int(a, s), W.to_int(b, self.size(e[2], scope, ex)[1])
            return W.const((x ** y if y >= 0 else 0) & ((1 << w) - 1), w)
        b = self.ev(e[2], w, s, scope, ex)
        if op in ("&", "|", "^", "~^"):
            return W.bitwise(op, a, b)
        if op == "+":
            return W.add(a, b)
        if op == "-":
            return W.sub(a, b)
        if op == "*":
            return W.mul(a, b)
        if op in ("/", "%"):
            if not (W.is_const(a) and W.is_const(b)):
                if W.is_const(b) and not s and W.to_int(b) > 0 and (W.to_int(b) & (W.to_int(b) - 1)) == 0:
                    k = W.to_int(b).bit_length() - 1      # power of two: shift / mask
                    return W.shift_const(a, k, False) if op == "/" else a[:k] + [0] * (w - k)
                raise _err(e, "unsupported: %s with non-constant operands" % op)
            x, y = W.to_int(a, s), W.to_int(b, s)
            if y == 0:
                return [0] * w
            q = abs(x) // abs(y) * (1 if (x >= 0) == (y >= 0) else -1)
            r = x - q * y
            return W.const((q if op == "/" else r) & ((1 << w) - 1), w)
        raise _err(e, "unsupported operator %s" % op)

    def call(self, e, w, s, scope, ex):
        W = self.W
        name, args = e[0], e[1]
        if name in ("$signed", "$unsigned"):
            if len(args) != 1:
                raise _err(e, "%s takes one argument" % name)
            aw = self.size(args[0], scope, ex)[0]
            return W.extend(self.ev(args[0], aw, False, scope, ex), w, s)
        if name == "$clog2":
            v = self.const_int(args[0], scope, ex)
            return W.extend(W.const(max(0, math.ceil(math.log2(v))) if v > 1 else 0, 32), w, s)
        if name.startswith("$"):
            raise _err(e, "unsupported system function %s" % name)
        f = scope.lookup(name)
        if not isinstance(f, Func):
            raise _err(e, "unknown function %s" % name)
        fn = f.node
        fname, rng, fsigned, inputs, decls, body = fn.a
        if len(args) != len(inputs):
            raise _err(e, "function %s takes %d arguments" % (name, len(inputs)))
        self.depth += 1
        if self.depth > 200:
            raise _err(e, "function recursion too deep")
        fs = Scope(f.scope, scope.prefix + "%s$%d." % (name, len(self.sigs)))
        fex = Exec(self, fs, "func", None)
        for (an, ar, asg), x in zip(inputs, args):
            if ar is not None:
                msb, lsb = self.const_int(ar[0], f.scope), self.const_int(ar[1], f.scope)
            else:
                msb, lsb = 0, 0
            sig = Sig(fs.prefix + an, msb, lsb, asg, "reg", fn, ar is None)
            fs.names[an] = sig
            xs = self.size(x, scope, ex)
            val = self.ev(x, max(sig.width, xs[0]), xs[1], scope, ex)[:sig.width]
            fex.env[sig.name] = tuple(val)
        if rng is not None:
            msb, lsb = self.const_int(rng[0], f.scope), self.const_int(rng[1], f.scope)
        else:
            msb, lsb = 0, 0
        ret = Sig(fs.prefix + fname, msb, lsb, fsigned, "reg", fn, rng is None)
        fs.names[fname] = ret
        fex.locals.add(ret.name)
        for d in decls:
            for dn, _ in d[1]:
                if d[0] == "integer":
                    ds = Sig(fs.prefix + dn, 31, 0, True, "integer", d, False)
                else:
                    r = d[2]
                    m_, l_ = (self.const_int(r[0], f.scope), self.const_int(r[1], f.scope)) if r else (0, 0)
                    ds = Sig(fs.prefix + dn, m_, l_, d[3], "reg", d, r is None)
                fs.names[dn] = ds
                fex.locals.add(ds.name)
        for sig in fs.names.values():
            fex.locals.add(sig.name)
        fex.run(body)
        vals = fex.env.get(ret.name)
        if vals is None or any(v is None or v == PARTIAL for v in vals):
            raise _err(fn, "function %s does not assign its result on every path" % fname)
        self.depth -= 1
        return W.extend(list(vals), w, s)


# ============================================================================= procedural code
class Exec:
    """Symbolic execution of procedural statements.

    env: sig name -> tuple of literals per bit (None: not written by this block yet);
    nb: same for non-blocking assignments (seq blocks), applied at the end."""

    def __init__(self, el, scope, mode, block):
        self.el, self.scope, self.mode, self.block = el, scope, mode, block
        self.env = {}
        self.nb = {}
        self.locals = set()
        self.cond_stack = []

    def final(self):
        """Seq blocks: next-state values (non-blocking over blocking)."""
        out = dict(self.env)
        for k, v in self.nb.items():
            if k in out:
                out[k] = tuple(b if b is not None else a for a, b in zip(out[k], v))
            else:
                out[k] = v
        return {k: v for k, v in out.items() if self.el.sigs.get(k) is not None and self.el.sigs[k].kind != "integer"}

    def is_target(self, sig, p):
        if self.block is None:
            return sig.name in self.locals
        t = self.block.targets.get(sig)
        return t is not None and p in t

    def read(self, sig, node):
        vals = self.env.get(sig.name)
        out = []
        for p in range(sig.width):
            v = vals[p] if vals is not None else None
            if v == PARTIAL:
                raise _err(node, "%s is read where it is only assigned on some paths (latch)" % sig.bitname(p))
            if v is None:
                if sig.kind == "integer" or sig.name in self.locals:
                    raise _err(node, "%s is read before it is assigned" % sig.bitname(p))
                if self.mode == "comb" and self.is_target(sig, p):
                    raise _err(node, "%s is read before it is assigned in this always block (latch / loop)"
                               % sig.bitname(p))
                v = self.el.resolve(sig, p, node)
            out.append(v)
        return out

    def run(self, st):
        el = self.el
        k = st.k
        if k == "block":
            if st[2]:
                sub = Scope(self.scope, self.scope.prefix + (st[0] or "blk%d" % id(st)) + ".")
                for d in st[2]:
                    for dn, _ in d[1]:
                        if d[0] == "integer":
                            s = Sig(sub.prefix + dn, 31, 0, True, "integer", d, False)
                        else:
                            r = d[2]
                            m_, l_ = (el.const_int(r[0], self.scope, self), el.const_int(r[1], self.scope, self)) if r else (0, 0)
                            s = Sig(sub.prefix + dn, m_, l_, d[3], "integer", d, r is None)
                        sub.names[dn] = s
                        el.sigs[s.name] = s
                saved = self.scope
                self.scope = sub
                for x in st[1]:
                    self.run(x)
                self.scope = saved
            else:
                for x in st[1]:
                    self.run(x)
        elif k in ("bassign", "nbassign"):
            self.assign(st[0], st[1], nonblocking=(k == "nbassign" and self.mode == "seq"), node=st)
        elif k == "if":
            c = el.W.bool(el.ev_self(st[0], self.scope, self))
            self.branch(c, st[1], st[2])
        elif k == "case":
            self.case(st)
        elif k == "for":
            self.loop(st)
        elif k == "null":
            pass
        else:
            raise _err(st, "unsupported statement %s" % k)

    def branch(self, c, a, b):
        if c == 1:
            if a is not None:
                self.run(a)
            return
        if c == 0:
            if b is not None:
                self.run(b)
            return
        env0, nb0 = dict(self.env), dict(self.nb)
        if a is not None:
            self.run(a)
        env_a, nb_a = self.env, self.nb
        self.env, self.nb = dict(env0), dict(nb0)
        if b is not None:
            self.run(b)
        self.env = self.merge(c, env_a, self.env)
        self.nb = self.merge(c, nb_a, self.nb, nb=True)

    def merge(self, c, A, B, nb=False):
        g = self.el.g
        out = {}
        for key in set(A) | set(B):
            va, vb = A.get(key), B.get(key)
            if va is vb:
                out[key] = va
                continue
            sig = self.el.sigs.get(key) or self.scope_sig(key)
            n = len(va if va is not None else vb)
            va = va or (None,) * n
            vb = vb or (None,) * n
            res = []
            for p, (x, y) in enumerate(zip(va, vb)):
                if x == y:
                    res.append(x)
                    continue
                if x is None or y is None:
                    if self.mode == "comb" or self.mode == "func" or (sig is not None and sig.kind == "integer"):
                        res.append(PARTIAL)
                        continue
                    if nb:
                        cur = self.env_value(sig, p, nb_default=True)
                    else:
                        cur = self.el.resolve(sig, p)
                    x = cur if x is None else x
                    y = cur if y is None else y
                if x == PARTIAL or y == PARTIAL:
                    res.append(PARTIAL)
                    continue
                res.append(g.MUX(c, x, y))
            out[key] = tuple(res)
        return out

    def env_value(self, sig, p, nb_default=False):
        """Default for a non-blocking target not written on one path: its current value (q)."""
        return self.el.resolve(sig, p)

    def scope_sig(self, key):
        return None

    def case(self, st):
        el, W, g = self.el, self.el.W, self.el.g
        kind, sel, items = st.a
        widths = [el.size(sel, self.scope, self)]
        for labels, _ in items:
            for lab in labels or []:
                widths.append(el.size(lab, self.scope, self))
        w = max(x[0] for x in widths)
        sgn = all(x[1] for x in widths)
        sv = el.ev(sel, w, sgn, self.scope, self)
        conds = []
        default = None
        for labels, body in items:
            if labels is None:
                default = body
                continue
            cs = []
            for lab in labels:
                if lab.k == "num":                 # x / z / ? bits are handled as masks below
                    lv = W.extend(W.const(lab[2] & ~(lab[3] | lab[4]), lab[0] or 32), w, sgn)
                else:
                    lv = el.ev(lab, w, sgn, self.scope, self)
                care = [True] * w
                if lab.k == "num":
                    xm, zm = lab[3], lab[4]
                    dc = (zm | xm) if kind == "casex" else zm if kind == "casez" else 0
                    if kind == "case" and (xm or zm):
                        cs.append(0)
                        continue
                    lw = lab[0] if lab[0] is not None else 32
                    for i in range(w):
                        src = i if i < lw else lw - 1
                        if (dc >> src) & 1:
                            care[i] = False
                cs.append(g.AND_N([g.XNOR(a, b) for a, b, c in zip(sv, lv, care) if c]))
            conds.append((g.OR_N(cs), body))
        if default is None and conds and g.is_const(g.OR_N([c for c, _ in conds])) == 1:
            default = conds.pop()[1]           # full case: the last item is the default
        self.chain(conds, default)

    def chain(self, conds, default):
        if not conds:
            if default is not None:
                self.run(default)
            return
        c, body = conds[0]
        if c == 1:
            self.run(body)
            return
        if c == 0:
            self.chain(conds[1:], default)
            return
        env0, nb0 = dict(self.env), dict(self.nb)
        self.run(body)
        env_a, nb_a = self.env, self.nb
        self.env, self.nb = dict(env0), dict(nb0)
        self.chain(conds[1:], default)
        self.env = self.merge(c, env_a, self.env)
        self.nb = self.merge(c, nb_a, self.nb, nb=True)

    def loop(self, st):
        el = self.el
        var, init, cond, slhs, srhs, body = st.a
        self.assign(N("id", var, at=st), init, False, st)
        n = 0
        while True:
            c = el.W.bool(el.ev_self(cond, self.scope, self))
            if c not in (0, 1):
                raise _err(st, "for loop condition is not constant (loops are unrolled)")
            if not c:
                break
            self.run(body)
            self.assign(slhs, srhs, False, st)
            n += 1
            if n > el.MAX_LOOP:
                raise _err(st, "for loop does not terminate")

    # ------------------------------------------------------------------ assignment
    def assign(self, lv, rhs, nonblocking, node):
        el = self.el
        tw = el.size(lv, self.scope, self)[0] if lv.k != "concat" else sum(el.size(x, self.scope, self)[0] for x in lv[0])
        sz = el.size(rhs, self.scope, self)
        val = el.ev(rhs, max(tw, sz[0]), sz[1], self.scope, self)[:tw]
        self.write(lv, val, nonblocking, node)

    def write(self, lv, val, nonblocking, node):
        el, W, g = self.el, self.el.W, self.el.g
        if lv.k == "concat":
            pos = 0
            for x in reversed(lv[0]):
                xw = el.size(x, self.scope, self)[0]
                self.write(x, val[pos:pos + xw], nonblocking, node)
                pos += xw
            return
        base = lv
        while base.k in ("index", "range", "prange"):
            base = base[0]
        sig = self.scope.lookup(base[0])
        if not isinstance(sig, Sig):
            raise _err(lv, "assignment to undeclared %s" % base[0])
        if self.block is not None and sig.kind != "integer" and sig not in self.block.targets:
            raise _err(lv, "internal: %s missing from block targets" % sig.name)
        store = self.nb if nonblocking else self.env
        if not nonblocking and sig.name in self.nb or nonblocking and sig.name in self.env \
                and sig.kind != "integer" and self.mode == "seq":
            raise _err(lv, "%s mixes blocking and non-blocking assignments" % sig.name)
        cur = list(store.get(sig.name) or (None,) * sig.width)
        if lv.k == "id":
            cur = list(val)
        elif lv.k in ("index", "range", "prange") and lv[0].k == "id":
            try:
                targets = [p for _, p in el.lv_targets(lv, self.scope, ex=self)]
                const = True
            except ElabError:
                const = False
            if const:
                for p, v in zip(targets, val):
                    cur[p] = v
            else:
                if lv.k != "index":
                    raise _err(lv, "unsupported: variable part select on the left-hand side")
                iw, isg = el.size(lv[1], self.scope, self)
                ib = W.extend(el.ev(lv[1], iw, isg, self.scope, self), 32, isg)
                off = W.sub(ib, W.const(sig.lsb & 0xFFFFFFFF, 32)) if sig.msb >= sig.lsb \
                    else W.sub(W.const(sig.lsb & 0xFFFFFFFF, 32), ib)
                for p in range(sig.width):
                    hit = W.eq(off, W.const(p, 32))
                    if hit == 0:
                        continue
                    old = cur[p]
                    if old is None:
                        if self.mode == "seq" and sig.kind != "integer":
                            old = el.resolve(sig, p) if not nonblocking else el.resolve(sig, p)
                        else:
                            old = PARTIAL if hit != 1 else None
                    if old == PARTIAL:
                        cur[p] = PARTIAL if hit != 1 else val[0]
                    else:
                        cur[p] = g.MUX(hit, val[0], old) if old is not None else val[0]
        else:
            raise _err(lv, "unsupported assignment target")
        store[sig.name] = tuple(cur)


# ============================================================================= entry points
def _run_big_stack(fn):
    out = {}

    def target():
        try:
            out["r"] = fn()
        except BaseException as e:            # re-raised in the caller's thread
            out["e"] = e
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 200000))
    threading.stack_size(512 * 1024 * 1024)
    t = threading.Thread(target=target)
    t.start()
    t.join()
    threading.stack_size(0)
    sys.setrecursionlimit(old)
    if "e" in out:
        raise out["e"]
    return out["r"]


def elaborate(paths=None, top=None, params=None, text=None, defines=None, include_dirs=()):
    """Parse and elaborate; `text` instead of `paths` for inline sources."""
    if text is not None:
        mods = parse_text(text, defines=defines)
    else:
        mods = parse_files([paths] if isinstance(paths, str) else paths, defines, include_dirs)
    if top is None:
        used = {it[0] for m in mods.values() for it in _walk_items(m[3]) if it.k == "inst"}
        tops = [n for n in mods if n not in used]
        if len(tops) != 1:
            raise ElabError("cannot pick the top module from %s: pass top=" % sorted(tops or mods))
        top = tops[0]
    return _run_big_stack(lambda: Elaborator(mods).run(top, params))


def _walk_items(items):
    for it in items:
        yield it
        if it.k in ("genfor",):
            yield from _walk_items(it[5])
        elif it.k == "genif":
            yield from _walk_items(it[1])
            yield from _walk_items(it[2])
        elif it.k == "genblock":
            yield from _walk_items(it[1])
