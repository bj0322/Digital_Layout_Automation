"""PDK-aware SPICE artifacts for every generated system (decoder/block, std-cell library, SRAM).

Everything here is optional and driven by the ``spice`` section of the tech file
(``tech/TECH_FILE.md``).  Nothing in this module knows a PDK: model files, supply voltage,
temperature and corner come from that section, and any value that is not configured is
reported as unavailable, never guessed.

    cfg = SpiceConfig.from_tech(tech)               # parse + validate (never raises on "absent")
    info = finalize_stdcell(lib, outdir, SpiceOptions(simulate="auto"))

Per artifact this writes ``<name>.spice.json`` (provenance, includes/corner/VDD/temperature,
sub-circuit and device counts, pins, simulation status and measurements) and the schematic SVGs
(``dla.schematic``).  Simulation is opt-in (``SpiceOptions.simulate``): ``off`` (default), ``auto``
(run when configured and a simulator exists, otherwise record why not) and ``require`` (anything
but a passing run is a failure).  The simulator is chosen exactly like :class:`dla.spice.SpiceRunner`
does: explicit command, ``$DLA_SPICE``, then ``ngspice`` on PATH.
"""
import hashlib
import itertools
import json
import math
import os
import re

from .spice import (SpiceDeck, SpiceError, SpiceRunner, SpiceUnavailable, parse_spice_number)
from .tech import _expand

SCHEMA = "dla-spice-metadata/1"
MODES = ("off", "auto", "require")
_KNOWN_KEYS = ("comment", "includes", "cell_library", "vdd", "temperature", "corner", "testbench")
_TB_DEFAULTS = {"max_vectors": 24, "high_fraction": 0.9, "low_fraction": 0.1}
_TB_KEYS = ("edge", "load", "max_vectors", "high_fraction", "low_fraction")


# ============================================================================= configuration
def _as_list(v, what):
    if v is None:
        return []
    if isinstance(v, (str, dict)):
        return [v]
    if isinstance(v, (list, tuple)):
        return list(v)
    raise ValueError("spice.%s must be a path or a list of paths" % what)


class SpiceConfig(object):
    """Parsed, validated ``spice`` tech section.  ``configured`` is False when it is absent."""

    def __init__(self, raw=None, base_dir=None, tech_name="tech"):
        self.tech_name = tech_name
        self.base_dir = base_dir or os.getcwd()
        self.configured = raw is not None
        raw = raw or {}
        if not isinstance(raw, dict):
            raise ValueError("tech %s: 'spice' must be an object" % tech_name)
        unknown = sorted(set(raw) - set(_KNOWN_KEYS))
        if unknown:
            raise ValueError("tech %s: unknown spice key(s): %s" % (tech_name, ", ".join(unknown)))
        self.includes = []
        for item in _as_list(raw.get("includes"), "includes"):
            if isinstance(item, str):
                item = {"file": item}
            if not isinstance(item, dict) or not isinstance(item.get("file"), str) or not item["file"].strip():
                raise ValueError("tech %s: each spice.includes entry is a path or {\"file\", \"section\"}" % tech_name)
            sec = item.get("section")
            if sec is not None and not (isinstance(sec, str) and re.match(r"^[A-Za-z_]\w*$", sec)):
                raise ValueError("tech %s: spice.includes section must be a simple name, got %r" % (tech_name, sec))
            self.includes.append({"file": item["file"].strip(), "section": sec})
        self.cell_library = []
        for item in _as_list(raw.get("cell_library"), "cell_library"):
            if not isinstance(item, str) or not item.strip():
                raise ValueError("tech %s: spice.cell_library entries must be path strings" % tech_name)
            self.cell_library.append(item.strip())
        self.vdd = self._number(raw, "vdd", positive=True)
        self.temperature = self._number(raw, "temperature")
        corner = raw.get("corner")
        if corner is not None and not isinstance(corner, str):
            raise ValueError("tech %s: spice.corner must be a string" % tech_name)
        self.corner = corner
        tb = raw.get("testbench", {})
        if not isinstance(tb, dict) or set(tb) - set(_TB_KEYS):
            raise ValueError("tech %s: spice.testbench takes only: %s" % (tech_name, ", ".join(_TB_KEYS)))
        self.testbench = dict(_TB_DEFAULTS)
        for k, v in tb.items():
            if k == "max_vectors":
                if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                    raise ValueError("tech %s: spice.testbench.max_vectors must be an integer >= 1" % tech_name)
                self.testbench[k] = v
            elif k in ("high_fraction", "low_fraction"):
                f = self._finite(v, "testbench." + k)
                if not 0.0 < f < 1.0:
                    raise ValueError("tech %s: spice.testbench.%s must be in (0, 1)" % (tech_name, k))
                self.testbench[k] = f
            else:
                f = self._finite(v, "testbench." + k)
                if f <= 0:
                    raise ValueError("tech %s: spice.testbench.%s must be positive" % (tech_name, k))
                self.testbench[k] = f
        if self.testbench["low_fraction"] >= self.testbench["high_fraction"]:
            raise ValueError("tech %s: spice.testbench.low_fraction must be below high_fraction" % tech_name)

    def _finite(self, v, what):
        try:
            return parse_spice_number(v)
        except ValueError:
            raise ValueError("tech %s: spice.%s must be a finite number (SPICE suffixes allowed), got %r"
                             % (self.tech_name, what, v))

    def _number(self, raw, key, positive=False):
        if raw.get(key) is None:
            return None
        v = self._finite(raw[key], key)
        if positive and v <= 0:
            raise ValueError("tech %s: spice.%s must be positive, got %r" % (self.tech_name, key, raw[key]))
        return v

    @classmethod
    def from_tech(cls, tech):
        """Config of a :class:`dla.tech.Tech` / :class:`dla.stdcell.rules.StdTech` (or a tech dict)."""
        if isinstance(tech, dict):
            return cls(tech.get("spice"), None, tech.get("name", "tech"))
        path = getattr(tech, "path", None)
        return cls(tech.raw.get("spice"), os.path.dirname(path) if path else None, tech.name)

    # ------------------------------------------------------------------ paths
    def resolve(self, raw_path):
        """``(absolute path, None)`` or ``(None, reason)`` when an environment variable is unset."""
        try:
            return _expand(raw_path, self.base_dir), None
        except EnvironmentError as e:
            return None, str(e)

    def readiness(self):
        """Reasons a model-based simulation cannot run (empty list = configured and files present)."""
        out = []
        if not self.includes:
            out.append("tech file has no spice.includes (device model files)")
        if self.vdd is None:
            out.append("tech file has no spice.vdd")
        for inc in self.includes:
            path, why = self.resolve(inc["file"])
            if path is None:
                out.append("spice.includes %s: %s" % (inc["file"], why))
            elif not os.path.isfile(path):
                out.append("model file not found: %s (from %s)" % (path, inc["file"]))
        return out

    def describe(self, hashes=True):
        """JSON-able view for metadata; includes are reported unexpanded plus their resolution."""
        def entry(raw, section=None):
            path, why = self.resolve(raw)
            e = {"file": raw, "resolved": path, "exists": bool(path and os.path.isfile(path))}
            if section:
                e["section"] = section
            if why:
                e["problem"] = why
            if hashes and e["exists"]:
                e["sha256"] = file_sha256(path)
            return e
        return {"configured": self.configured, "corner": self.corner, "vdd_v": self.vdd,
                "temperature_c": self.temperature,
                "includes": [entry(i["file"], i["section"]) for i in self.includes],
                "cell_library": [entry(p) for p in self.cell_library],
                "testbench": dict(self.testbench)}


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SpiceOptions(object):
    """How an artifact run treats SPICE.  ``simulate``: off | auto | require."""

    def __init__(self, simulate="off", command=None, timeout=120, schematic=True, workdir=None):
        if simulate not in MODES:
            raise ValueError("simulate must be one of %s, got %r" % ("/".join(MODES), simulate))
        self.simulate, self.command, self.timeout = simulate, command, timeout
        self.schematic, self.workdir = bool(schematic), workdir


# ============================================================================= netlist parsing
class Mos(object):
    __slots__ = ("name", "kind", "g", "a", "b", "bulk", "model", "params")

    def __init__(self, name, kind, g, a, b, bulk, model, params):
        self.name, self.kind, self.g, self.a, self.b = name, kind, g, a, b
        self.bulk, self.model, self.params = bulk, model, params


class Subckt(object):
    def __init__(self, name, pins):
        self.name, self.pins = name, pins
        self.mos, self.insts, self.other, self.lines = [], [], [], []

    @property
    def text(self):
        return "\n".join(self.lines) + "\n"


def _logical_lines(text):
    out = []
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("+") and out:
            out[-1][0] += " " + s[1:].strip()
            out[-1][1].append(raw.rstrip())
        else:
            out.append([s, [raw.rstrip()]])
    return out


def _params(tokens):
    d = {}
    for t in tokens:
        if "=" in t:
            k, _, v = t.partition("=")
            d[k.lower()] = v
    return d


def parse_netlist(text, vdd="VDD", vss="VSS"):
    """Sub-circuit definitions in SPICE/CDL ``text`` -> ordered dict ``name -> Subckt``.

    MOS devices become :class:`Mos` (type from the bulk net, else from the model name); X lines
    become ``(name, nets, subckt)`` tuples; any other element letter is only counted.
    """
    subs, cur = {}, None
    low_power = {str(vdd).lower(): "p", str(vss).lower(): "n"}
    for line, raws in _logical_lines(text):
        for sep in ("$", ";"):
            if sep in line:
                line = line.split(sep, 1)[0].strip()
        if not line or line[0] == "*":
            if cur is not None:
                cur.lines.extend(raws)
            continue
        tok = line.split()
        head = tok[0].lower()
        if head == ".subckt" and len(tok) >= 2:
            pins = []
            for t in tok[2:]:
                if "=" in t or t.lower() == "params:":
                    break
                pins.append(t)
            cur = Subckt(tok[1], pins)
            subs[tok[1]] = cur
            cur.lines.extend(raws)
        elif head == ".ends" and cur is not None:
            cur.lines.extend(raws)
            cur = None
        elif cur is not None:
            cur.lines.extend(raws)
            if head[0] == "m" and len(tok) >= 6:
                d, g, s, b, model = tok[1:6]
                kind = low_power.get(b.lower())
                if kind is None:
                    kind = "p" if re.search(r"pmos|pfet|(^|[_.])p", model.lower()) else "n"
                cur.mos.append(Mos(tok[0], kind, g, d, s, b, model, _params(tok[6:])))
            elif head[0] == "x" and len(tok) >= 3:
                nets = [t for t in tok[1:] if "=" not in t]
                cur.insts.append((tok[0], nets[:-1], nets[-1]))
            elif head[0] != ".":
                cur.other.append(tok[0])
    return subs


def lookup(subs, name):
    """Sub-circuit by exact name, else case-insensitively (SPICE names are case-insensitive)."""
    if name in subs:
        return subs[name]
    low = name.lower()
    for k in subs:
        if k.lower() == low:
            return subs[k]
    return None


def count_devices(subs, top, _memo=None):
    """Flattened device counts below sub-circuit ``top``: ``{"nmos": n, "pmos": n, "other": n}``."""
    memo = {} if _memo is None else _memo
    sub = lookup(subs, top)
    if sub is None:
        return {"nmos": 0, "pmos": 0, "other": 0}
    key = sub.name
    if key in memo:
        return memo[key]
    memo[key] = {"nmos": 0, "pmos": 0, "other": 0}          # recursion guard
    c = {"nmos": sum(1 for m in sub.mos if m.kind == "n"), "pmos": sum(1 for m in sub.mos if m.kind == "p"),
         "other": len(sub.other)}
    for _, _, child in sub.insts:
        for k, v in count_devices(subs, child, memo).items():
            c[k] += v
    memo[key] = c
    return c


def describe_netlist(subs, top, vdd="VDD", vss="VSS"):
    """Metadata block for a parsed netlist: sub-circuit/device counts and the top-level pins."""
    t = lookup(subs, top)
    return {"top": top, "subckt_count": len(subs),
            "subckts": {n: {"pins": s.pins, "mos": len(s.mos), "instances": len(s.insts)} for n, s in subs.items()},
            "top_pins": list(t.pins) if t else None,
            "top_instances": len(t.insts) if t else 0,
            "devices": count_devices(subs, top) if t else None,
            "supplies": {"vdd": vdd, "vss": vss}}


# ----------------------------------------------------------------------------- classification
_KNOWN_TABLES = {}


def _reference_functions():
    if not _KNOWN_TABLES:
        for n in (1, 2, 3, 4):
            vecs = list(itertools.product((0, 1), repeat=n))
            _KNOWN_TABLES[("inv" if n == 1 else "nand%d" % n, n)] = tuple(int(not all(v)) for v in vecs)
            if n > 1:
                _KNOWN_TABLES[("nor%d" % n, n)] = tuple(int(not any(v)) for v in vecs)
                _KNOWN_TABLES[("and%d" % n, n)] = tuple(int(all(v)) for v in vecs)
                _KNOWN_TABLES[("or%d" % n, n)] = tuple(int(any(v)) for v in vecs)
    return _KNOWN_TABLES


def classify_cell(sub, vdd="VDD", vss="VSS"):
    """Logic function of a leaf transistor sub-circuit by switch-level simulation.

    Returns ``{"kind": "inv"|"nand2"|...|"complex"|"unknown", "inputs": [...], "output": pin,
    "table": [...]}``; ``unknown`` when the sub-circuit has no MOS devices or no single output.
    """
    from .stdcell.netlist import switch_eval
    if not sub.mos or sub.insts:
        return {"kind": "unknown", "inputs": [], "output": None, "table": None}
    sup = {vdd.lower(), vss.lower()}
    gates = {m.g for m in sub.mos}
    diff = {n for m in sub.mos for n in (m.a, m.b)}
    pins = [p for p in sub.pins if p.lower() not in sup]
    outs = [p for p in pins if p in diff and p not in gates]
    ins = [p for p in pins if p in gates and p not in diff]
    if len(outs) != 1 or not ins or len(ins) > 5:
        return {"kind": "unknown", "inputs": ins, "output": outs[0] if outs else None, "table": None}
    table = []
    for vec in itertools.product((0, 1), repeat=len(ins)):
        val = switch_eval(sub.mos, dict(zip(ins, vec)), vdd, vss)
        table.append(val.get(outs[0]) if val.get(outs[0]) in (0, 1) else None)
    kind = "complex"
    if None in table:
        kind = "unknown"
    else:
        for (name, n), ref in _reference_functions().items():
            if n == len(ins) and ref == tuple(table):
                kind = name
                break
    return {"kind": kind, "inputs": ins, "output": outs[0], "table": table}


# ============================================================================= structural SPICE
def load_cell_library(config, vdd="VDD", vss="VSS"):
    """Parse the configured ``spice.cell_library`` files: ``(subckts, problems)``."""
    subs, problems = {}, []
    for raw in config.cell_library:
        path, why = config.resolve(raw)
        if path is None:
            problems.append("spice.cell_library %s: %s" % (raw, why))
        elif not os.path.isfile(path):
            problems.append("cell library file not found: %s" % path)
        else:
            with open(path, errors="replace") as f:
                subs.update(parse_netlist(f.read(), vdd, vss))
    return subs, problems


def _port_nets(ports):
    out = []
    for name, _direction, width in ports:
        out += [name] if width == 1 else ["%s[%d]" % (name, i) for i in range(width)]
    return out


def block_netlist(blk, config):
    """Structural SPICE of a placed block from the configured real cell sub-circuits.

    Returns ``{"status": "ok"|"skipped", "reason", "text", "top", "pins", "cells_used", "missing",
    "dangling"}``.  Never fabricates a cell: a missing sub-circuit skips the netlist and says which.
    """
    t = blk.t
    if not config.cell_library:
        return {"status": "skipped", "reason": "tech file has no spice.cell_library", "text": None,
                "top": blk.name, "pins": None, "cells_used": [], "missing": [], "dangling": []}
    subs, problems = load_cell_library(config, t.vdd, t.vss)
    if problems or not subs:
        return {"status": "skipped", "reason": "; ".join(problems) or "cell library defines no sub-circuits",
                "text": None, "top": blk.name, "pins": None, "cells_used": [], "missing": [], "dangling": []}
    pins_of = {}
    for net, ips in blk.conn.items():
        for inst, pin in ips:
            pins_of.setdefault(inst, {})[pin] = net
    lines, used, missing, dangling = [], [], [], []
    for name, cell, _x, _y, _o in blk.insts:
        if name not in pins_of:
            continue
        sub = lookup(subs, cell)
        if sub is None:
            if cell not in missing:
                missing.append(cell)
            continue
        if sub.name not in used:
            used.append(sub.name)
        low = {k.lower(): v for k, v in pins_of[name].items()}
        nets = []
        for p in sub.pins:
            if p in pins_of[name]:
                nets.append(pins_of[name][p])
            elif p.lower() in low:
                nets.append(low[p.lower()])
            elif p.lower() == t.vdd.lower():
                nets.append(t.vdd)
            elif p.lower() == t.vss.lower():
                nets.append(t.vss)
            else:
                nets.append("nc_%s_%s" % (name, p))
                dangling.append("%s.%s" % (name, p))
        lines.append("X%s %s %s" % (name, " ".join(nets), sub.name))
    if missing:
        return {"status": "skipped", "reason": "cell(s) not in the SPICE cell library: %s" % ", ".join(sorted(missing)),
                "text": None, "top": blk.name, "pins": None, "cells_used": used, "missing": sorted(missing),
                "dangling": dangling}
    pins = _port_nets(blk.ports) + [t.vdd, t.vss]
    out = ["* %s: structural netlist (%s) generated by Digital_Layout_Auto, tech %s"
           % (blk.name, blk.meta.get("block", "block"), t.name),
           "* cell sub-circuits are copied verbatim from the configured spice.cell_library", ""]
    for n in sorted(used):
        out.append(lookup(subs, n).text)
    out += [".SUBCKT %s %s" % (blk.name, " ".join(pins))] + lines + [".ENDS %s" % blk.name, ""]
    return {"status": "ok", "reason": None, "text": "\n".join(out), "top": blk.name, "pins": pins,
            "cells_used": sorted(used), "missing": [], "dangling": dangling}


# ============================================================================= simulation core
def _fwd(path):
    return os.path.abspath(path).replace("\\", "/")


class _Session(object):
    """One simulation request: config + runner + the deck prelude shared by all artifact kinds."""

    def __init__(self, options, config, workdir):
        self.options, self.config, self.workdir = options, config, workdir
        self.runner = SpiceRunner(options.command, timeout=options.timeout)
        self.decks = []

    def problems(self):
        out = self.config.readiness()
        if not self.runner.available:
            out.insert(0, "no SPICE simulator (put ngspice on PATH, set DLA_SPICE, or pass --spice-cmd)"
                       if not self.runner.command else
                       "SPICE executable %r (from %s) not found" % (self.runner.command[0], self.runner.source))
        return out

    def deck(self, title, vss_node="0"):
        d = SpiceDeck(title)
        for inc in self.config.includes:
            path, _ = self.config.resolve(inc["file"])
            if inc["section"]:
                d.add('.lib "%s" %s' % (_fwd(path), inc["section"]))
            else:
                d.include(path)
        if self.config.temperature is not None:
            d.add(".temp %g" % self.config.temperature)
        return d

    def run(self, deck, name, required):
        res = self.runner.run(deck, self.workdir, name, required=required)
        self.decks.append({"name": name, "deck": res.deck_path, "log": res.log_path})
        return res


def _dc_trigger(deck):
    deck.add("VDUM dum 0 0", "RDUM dum 0 1k").dc("VDUM", 0, 1, 1)
    return deck


def _check(name, passed, **kw):
    d = {"name": name, "passed": bool(passed)}
    d.update(kw)
    return d


def _logic_ok(v, expect_high, vdd, tb):
    return v >= tb["high_fraction"] * vdd if expect_high else v <= tb["low_fraction"] * vdd


def sim_stdcell(sess, cells, spice_path, st):
    """DC truth tables of every generated logic cell (+ opt-in delay arcs) with the PDK models."""
    vdd, tb = sess.config.vdd, sess.config.testbench
    deck = sess.deck("stdcell truth tables")
    deck.include(spice_path).add("VSUP vdd 0 %g" % vdd)
    plan = []
    for ci, cl in enumerate(c for c in cells if c.netlist is not None):
        cn = cl.netlist
        for j, vec in enumerate(itertools.product((0, 1), repeat=len(cn.inputs))):
            tag = "c%d_%d" % (ci, j)
            conn = {cn.output: "y_" + tag, cn.vdd: "vdd", cn.vss: "0"}
            for p, v in zip(cn.inputs, vec):
                conn[p] = "n%s_%s" % (tag, p)
                deck.add("V%s_%s %s 0 %g" % (tag, p, conn[p], vdd * v))
            deck.add("X%s %s %s" % (tag, " ".join(conn[p] for p in cn.pins), cn.name))
            want = int(bool(cn.func(dict(zip(cn.inputs, vec)))))
            nm = ("hi_" if want else "lo_") + tag
            deck.measure("dc", nm, "FIND v(y_%s) AT=0" % tag)
            plan.append((cn.name, vec, want, nm))
    _dc_trigger(deck)
    res = sess.run(deck, "stdcell_truth", [p[3] for p in plan])
    by_cell, checks, meas = {}, [], []
    for cell, vec, want, nm in plan:
        by_cell.setdefault(cell, []).append((vec, want, res.require(nm)))
    for cell, rows in by_cell.items():
        bad = [("".join(map(str, vec)), v) for vec, want, v in rows if not _logic_ok(v, want, vdd, tb)]
        highs = [v for _, w, v in rows if w]
        lows = [v for _, w, v in rows if not w]
        checks.append(_check(cell + ".truth_table", not bad, vectors=len(rows), failed=[
            {"inputs": b, "v_out": v} for b, v in bad]))
        if highs:
            meas.append({"name": cell + ".v_out_high_min", "value": min(highs), "unit": "V"})
        if lows:
            meas.append({"name": cell + ".v_out_low_max", "value": max(lows), "unit": "V"})
    delays, skipped = _stdcell_delays(sess, cells, spice_path, vdd, tb)
    return {"checks": checks, "measurements": meas, "delays": delays, "skipped": skipped,
            "conditions": {"vdd_v": vdd, "logic_high_fraction": tb["high_fraction"],
                           "logic_low_fraction": tb["low_fraction"]}}


def _stdcell_delays(sess, cells, spice_path, vdd, tb):
    if "edge" not in tb or "load" not in tb:
        return [], ["delay arcs: tech spice.testbench has no edge/load (not measured)"]
    edge, load = tb["edge"], tb["load"]
    pw = 100 * edge
    deck = sess.deck("stdcell delay arcs")
    deck.include(spice_path).add("VSUP vdd 0 %g" % vdd)
    plan = []
    for ci, cl in enumerate(c for c in cells if c.netlist is not None):
        cn = cl.netlist
        for pi, pin in enumerate(cn.inputs):
            others = [p for p in cn.inputs if p != pin]
            for rest in itertools.product((0, 1), repeat=len(others)):
                base = dict(zip(others, rest))
                lo = bool(cn.func(dict(base, **{pin: 0})))
                hi = bool(cn.func(dict(base, **{pin: 1})))
                if lo != hi:
                    break
            else:
                continue
            tag = "c%d_%d" % (ci, pi)
            conn = {cn.output: "y_" + tag, cn.vdd: "vdd", cn.vss: "0", pin: "in_" + tag}
            deck.add("VIN_%s in_%s 0 PULSE(0 %g %g %g %g %g %g)" % (tag, tag, vdd, edge, edge, edge, pw, 4 * pw))
            for p, v in base.items():
                conn[p] = "h%s_%s" % (tag, p)
                deck.add("VH%s_%s %s 0 %g" % (tag, p, conn[p], vdd * v))
            deck.add("X%s %s %s" % (tag, " ".join(conn[p] for p in cn.pins), cn.name),
                     "CL%s y_%s 0 %g" % (tag, tag, load))
            for edge_in, trig in (("rise", "RISE"), ("fall", "FALL")):
                out_rise = hi if edge_in == "rise" else lo
                nm = "d%s_%s" % (edge_in[0], tag)
                deck.measure("tran", nm, "TRIG v(in_%s) VAL=%g %s=1 TARG v(y_%s) VAL=%g %s=1"
                             % (tag, vdd / 2, trig, tag, vdd / 2, "RISE" if out_rise else "FALL"))
                plan.append({"cell": cn.name, "input": pin, "input_edge": edge_in,
                             "output_edge": "rise" if out_rise else "fall", "side_inputs": dict(base), "_m": nm})
    deck.tran(edge / 10.0, 4 * pw + 2 * edge)
    res = sess.run(deck, "stdcell_delay", [p["_m"] for p in plan])
    out = []
    for p in plan:
        p = dict(p)
        p["delay_s"] = res.require(p.pop("_m"))
        p["load_f"], p["edge_s"] = load, edge
        out.append(p)
    return out, []


def select_vectors(width, enable, cap):
    """Deterministic decoder test vectors ``(address, en)``: all of them when few, else corners
    plus walking ones/zeros (and EN=0 corners)."""
    n = 1 << width
    full = [(a, e) for e in ((1, 0) if enable else (1,)) for a in range(n)]
    if len(full) <= cap:
        return full
    pick = [0, n - 1] + [1 << b for b in range(width)] + [(n - 1) ^ (1 << b) for b in range(width)]
    vecs = []
    for a in pick:
        if (a, 1) not in vecs:
            vecs.append((a, 1))
    if enable:
        vecs += [(0, 0), (n - 1, 0)]
    return vecs[:cap]


def sim_decoder(sess, blk, netlist, spice_path):
    """Per-vector DC check of the structural decoder netlist: the addressed output is high."""
    vdd, tb = sess.config.vdd, sess.config.testbench
    n, en = blk.meta["n_bits"], blk.meta["enable"]
    N = 1 << n
    vectors = select_vectors(n, en, tb["max_vectors"])
    checks, worst_hi, worst_lo, failed = [], [], [], []
    for vi, (addr, e) in enumerate(vectors):
        deck = sess.deck("%s vector %d" % (blk.name, vi))
        deck.include(spice_path).add("VSUP vdd 0 %g" % vdd)
        conn = {}
        for b in range(n):
            deck.add("VA%d a%d 0 %g" % (b, b, vdd * ((addr >> b) & 1)))
        if en:
            deck.add("VEN en 0 %g" % (vdd * e))
        want = addr if (e or not en) else None
        if N <= 64:
            outs = list(range(N))
        else:
            outs = sorted(set([addr, 0, N - 1] + [addr ^ (1 << b) for b in range(n)]))
        pins = []
        for p in netlist["pins"]:
            m = re.match(r"^(\w+)\[(\d+)\]$", p)
            if p in (blk.t.vdd, blk.t.vss):
                pins.append("vdd" if p == blk.t.vdd else "0")
            elif m and m.group(1) == "A":
                pins.append("a" + m.group(2))
            elif p == "EN":
                pins.append("en")
            elif m and m.group(1) == "Y":
                pins.append("yo" + m.group(2))
            else:
                pins.append("nc_" + re.sub(r"\W", "_", p))
        deck.add("Xdut %s %s" % (" ".join(pins), blk.name))
        names = []
        for k in outs:
            hi = want == k
            nm = ("hi_" if hi else "lo_") + str(k)
            deck.measure("dc", nm, "FIND v(yo%d) AT=0" % k)
            names.append((k, hi, nm))
        _dc_trigger(deck)
        res = sess.run(deck, "dec_vec%03d" % vi, [x[2] for x in names])
        for k, hi, nm in names:
            v = res.require(nm)
            (worst_hi if hi else worst_lo).append(v)
            if not _logic_ok(v, hi, vdd, tb):
                failed.append({"address": addr, "enable": e, "output": k, "v_out": v, "expected_high": hi})
    checks.append(_check("%s.decode" % blk.name, not failed, vectors=len(vectors), outputs_per_vector=len(outs),
                         failed=failed[:20], failed_count=len(failed)))
    meas = []
    if worst_hi:
        meas.append({"name": "selected_output_high_min", "value": min(worst_hi), "unit": "V"})
    meas.append({"name": "unselected_output_low_max", "value": max(worst_lo), "unit": "V"})
    return {"checks": checks, "measurements": meas, "delays": [], "skipped": [],
            "conditions": {"vdd_v": vdd, "vectors": ["A=%d,EN=%d" % v if en else "A=%d" % v[0] for v in vectors],
                           "outputs_checked": "all" if N <= 64 else "addressed output + %d neighbours" % (len(outs) - 1),
                           "logic_high_fraction": tb["high_fraction"], "logic_low_fraction": tb["low_fraction"]}}


def sim_sram_cell(sess, cell_netlist, spice_path):
    """DC hold / read-disturb / write checks of the 6T bit-cell with the PDK models (``.nodeset`` picks
    the stored state; a write succeeds when the old state no longer exists as a DC solution)."""
    vdd, tb = sess.config.vdd, sess.config.testbench
    cn = cell_netlist
    deck = sess.deck("sram bitcell DC")
    deck.include(spice_path).add("VSUP vdd 0 %g" % vdd)
    scen = [("hold1", 0, vdd, vdd, 1), ("hold0", 0, vdd, vdd, 0), ("read1", vdd, vdd, vdd, 1),
            ("read0", vdd, vdd, vdd, 0), ("write0", vdd, 0, vdd, 1), ("write1", vdd, vdd, 0, 0)]
    for nm, wl, bl, blb, q0 in scen:
        deck.add("VWL_%s wl_%s 0 %g" % (nm, nm, wl), "VBL_%s bl_%s 0 %g" % (nm, nm, bl),
                 "VBLB_%s blb_%s 0 %g" % (nm, nm, blb))
        conn = {"BL": "bl_" + nm, "BLB": "blb_" + nm, "WL": "wl_" + nm, cn.vdd: "vdd", cn.vss: "0"}
        deck.add("X%s %s %s" % (nm, " ".join(conn[p] for p in cn.pins), cn.name))
        deck.add(".nodeset v(x%s.q)=%g v(x%s.qb)=%g" % (nm, vdd * q0, nm, vdd * (1 - q0)))
        deck.measure("dc", "q_" + nm, "FIND v(x%s.q) AT=0" % nm)
        deck.measure("dc", "qb_" + nm, "FIND v(x%s.qb) AT=0" % nm)
    _dc_trigger(deck)
    res = sess.run(deck, "sram_bitcell", ["%s_%s" % (a, nm) for nm, *_ in scen for a in ("q", "qb")])
    checks, meas = [], []
    for nm, wl, bl, blb, q0 in scen:
        q, qb = res.require("q_" + nm), res.require("qb_" + nm)
        want_q = {"hold1": 1, "hold0": 0, "read1": 1, "read0": 0, "write0": 0, "write1": 1}[nm]
        hi_node, lo_node = (q, qb) if want_q else (qb, q)
        ok = hi_node >= tb["high_fraction"] * vdd and lo_node <= tb["low_fraction"] * vdd
        if nm.startswith("read"):       # retention under read disturb: no flip, bump below VDD/2
            ok = hi_node > vdd / 2 > lo_node
            meas.append({"name": nm + ".read_bump_v", "value": lo_node, "unit": "V"})
        checks.append(_check("bitcell." + nm, ok, v_q=q, v_qb=qb, expect_q=want_q))
        meas.append({"name": nm + ".v_q", "value": q, "unit": "V"})
        meas.append({"name": nm + ".v_qb", "value": qb, "unit": "V"})
    return {"checks": checks, "measurements": meas, "delays": [],
            "skipped": ["transient write/read timing, SNM and power are not measured (DC state checks only)"],
            "conditions": {"vdd_v": vdd, "logic_high_fraction": tb["high_fraction"],
                           "logic_low_fraction": tb["low_fraction"]}}


def run_simulation(options, config, workdir, fn, *args):
    """Execute ``fn(session, *args)`` under the opt-in policy; always returns a JSON-able report.

    status: ``not_requested`` | ``unavailable`` (cannot run: reason says why) | ``passed`` | ``failed``.
    """
    base = {"requested": options.simulate, "status": "not_requested", "reason": None}
    if options.simulate == "off":
        base["reason"] = "simulation not requested (use --simulate auto|require)"
        return base
    try:
        sess = _Session(options, config, workdir)
    except ValueError as e:
        base.update(status="failed", reason="invalid simulator options: %s" % e)
        return base
    base["simulator"] = {"command": list(sess.runner.command), "executable": sess.runner.executable,
                         "source": sess.runner.source}
    problems = sess.problems()
    if problems:
        base.update(status="unavailable", reason="; ".join(problems))
        return base
    os.makedirs(workdir, exist_ok=True)
    try:
        out = fn(sess, *args)
    except SpiceUnavailable as e:
        base.update(status="unavailable", reason=str(e))
        return base
    except SpiceError as e:
        base.update(status="failed", reason="%s: %s" % (type(e).__name__, e), decks=sess.decks)
        return base
    failed = [c["name"] for c in out["checks"] if not c["passed"]]
    base.update(status="failed" if failed else "passed", decks=sess.decks, checks=out["checks"],
                measurements=out["measurements"], delays=out["delays"], not_measured=out["skipped"],
                conditions=out["conditions"],
                reason=("%d of %d check(s) failed: %s" % (len(failed), len(out["checks"]), ", ".join(failed[:8])))
                if failed else None)
    return base


def policy_ok(sim):
    """Exit policy: off never fails, auto tolerates 'unavailable', require needs 'passed'; 'failed' always fails."""
    mode, st = sim["requested"], sim["status"]
    if mode == "off":
        return True
    if st == "failed":
        return False
    return st == "passed" or (mode == "auto" and st == "unavailable")


def summary_line(sim):
    """One-line console summary of a simulation report."""
    st = sim["status"]
    if st == "passed":
        return "[spice] PASS: %d check(s), %d measurement(s), %d delay arc(s)" % (
            len(sim["checks"]), len(sim["measurements"]), len(sim["delays"]))
    if st == "not_requested":
        return "[spice] simulation not requested (--simulate auto|require to run it)"
    tag = {"failed": "FAIL", "unavailable": "UNAVAILABLE" if sim["requested"] == "require" else "skipped"}[st]
    return "[spice] %s: %s" % (tag, sim["reason"])


# ============================================================================= metadata
def build_metadata(kind, name, tech, config, netlist=None, sim=None, extra=None):
    from . import __version__
    tech_path = getattr(tech, "path", None)
    meta = {"schema": SCHEMA,
            "artifact": {"kind": kind, "name": name},
            "provenance": {"generator": "Digital_Layout_Auto", "version": __version__, "tech": tech.name,
                           "tech_file": os.path.basename(tech_path) if tech_path else None,
                           "tech_sha256": file_sha256(tech_path) if tech_path and os.path.isfile(tech_path) else None},
            "spice_config": config.describe(),
            "netlist": netlist,
            "simulation": sim or {"requested": "off", "status": "not_requested", "reason": "simulation not requested"}}
    if extra:
        meta.update(extra)
    return meta


def write_json(path, data):
    with open(path, "w", newline="\n") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def _netlist_info(path, top, vdd, vss):
    if not path or not os.path.isfile(path):
        return {"status": "skipped", "file": None}, {}
    with open(path, errors="replace") as f:
        subs = parse_netlist(f.read(), vdd, vss)
    info = describe_netlist(subs, top, vdd, vss)
    info.update(status="ok", file=os.path.basename(path))
    return info, subs


def _sim_dir(outdir, options):
    return options.workdir or os.path.join(outdir, "spice_sim")


# ============================================================================= per-artifact drivers
def finalize_stdcell(lib, outdir, options=None):
    """Metadata JSON + schematics (+ opt-in simulation) for a library from ``make_library``."""
    from . import schematic
    options = options or SpiceOptions()
    os.makedirs(outdir, exist_ok=True)
    st, name, p = lib["st"], lib["name"], lib["paths"]
    config = SpiceConfig.from_tech(st)
    info, subs = _netlist_info(p["spice"], name, st.vdd, st.vss)
    sim = run_simulation(options, config, _sim_dir(outdir, options), sim_stdcell, lib["cells"], p["spice"], st)
    out = {"sim": sim, "paths": {}}
    meta = build_metadata("stdcell", name, st, config, info, sim,
                          {"cells": {cl.name: {"function": classify_cell(lookup(subs, cl.name), st.vdd, st.vss)["kind"],
                                               "devices": len(cl.netlist.devices), "inputs": list(cl.netlist.inputs),
                                               "outputs": cl.netlist.outputs} for cl in lib["cells"]
                                     if cl.netlist is not None}})
    out["paths"]["spice_json"] = write_json(os.path.join(outdir, name + ".spice.json"), meta)
    if options.schematic:
        out["paths"].update(schematic.write_library_schematics(outdir, name, subs, st, config, sim))
    out["ok"] = policy_ok(sim)
    return out


def finalize_block(blk, outdir, paths, options=None):
    """Structural SPICE, metadata JSON and schematic for a placed block (see ``export.write_all``)."""
    from . import schematic
    options = options or SpiceOptions()
    os.makedirs(outdir, exist_ok=True)
    t = blk.t
    config = SpiceConfig.from_tech(t)
    netlist = block_netlist(blk, config)
    out = {"paths": {}}
    info, subs = {"status": netlist["status"], "reason": netlist["reason"], "file": None}, {}
    if netlist["status"] == "ok":
        sp = os.path.join(outdir, blk.name + ".spice")
        with open(sp, "w", newline="\n") as f:
            f.write(netlist["text"])
        out["paths"]["spice"] = sp
        info, subs = _netlist_info(sp, blk.name, t.vdd, t.vss)
        info.update(cells_used=netlist["cells_used"], dangling_pins=netlist["dangling"])
    if options.simulate == "off":
        sim = run_simulation(options, config, None, None)
    elif netlist["status"] != "ok":
        sim = {"requested": options.simulate, "status": "unavailable",
               "reason": "no structural netlist: %s" % netlist["reason"]}
    elif blk.meta.get("block") != "decoder":
        sim = {"requested": options.simulate, "status": "unavailable",
               "reason": "no functional testbench for block type %r" % blk.meta.get("block")}
    else:
        sim = run_simulation(options, config, _sim_dir(outdir, options), sim_decoder, blk, netlist,
                             out["paths"]["spice"])
    meta = build_metadata("block", blk.name, t, config, info, sim,
                          {"block": {"type": blk.meta.get("block"), "instances": len(blk.insts),
                                     "ports": [list(p) for p in blk.ports]}})
    out["paths"]["spice_json"] = write_json(os.path.join(outdir, blk.name + ".spice.json"), meta)
    if options.schematic:
        out["paths"].update(schematic.write_block_schematic(outdir, blk, subs, config, sim))
    out["sim"], out["ok"] = sim, policy_ok(sim)
    return out


def finalize_sram(res, outdir, options=None):
    """Metadata JSON + schematics (+ opt-in bit-cell simulation) for a ``make_sram`` result."""
    from . import schematic
    options = options or SpiceOptions()
    os.makedirs(outdir, exist_ok=True)
    st, name, p, cell = res["st"], res["name"], res["paths"], res["cell"]
    config = SpiceConfig.from_tech(st)
    info, subs = _netlist_info(p["spice"], name, st.vdd, st.vss)
    sim = run_simulation(options, config, _sim_dir(outdir, options), sim_sram_cell, cell.netlist, p["spice"])
    arr = res["array"]
    meta = build_metadata("sram", name, st, config, info, sim,
                          {"sram": {"rows": arr.rows, "cols": arr.cols, "bitcell": cell.name,
                                    "bitcell_devices": len(cell.netlist.devices),
                                    "sizing_um": {"w_pu": res["sizing"].w_pu, "w_pg": res["sizing"].w_pg,
                                                  "w_pd": res["sizing"].w_pd, "l": res["sizing"].l},
                                    "note": "sizing metrics in <name>.json are a heuristic ratio model, not SPICE"}})
    out = {"paths": {"spice_json": write_json(os.path.join(outdir, name + ".spice.json"), meta)}}
    if options.schematic:
        out["paths"].update(schematic.write_sram_schematics(outdir, name, subs, res, config, sim))
    out["sim"], out["ok"] = sim, policy_ok(sim)
    return out
