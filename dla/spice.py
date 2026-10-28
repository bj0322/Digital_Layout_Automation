"""PDK-independent SPICE testbench builder and ngspice batch runner.

This module knows nothing about SRAM, standard cells or a particular PDK.  It is the
execution layer later characterization code builds on: compose a :class:`SpiceDeck`,
run it with :class:`SpiceRunner`, keep the deck/log on disk, and read ``.measure``
results back as finite Python floats.
"""
from dataclasses import dataclass, field
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile

__all__ = ["SpiceError", "SpiceUnavailable", "SpiceTimeout", "SpiceFailure", "SpiceMeasureError",
           "SpiceDeck", "SpiceRunner", "SpiceResult", "parse_spice_number", "parse_measures",
           "smoke_testbench"]


class SpiceError(RuntimeError):
    """Base class; ``result`` holds the :class:`SpiceResult` when a run got that far."""

    def __init__(self, message, result=None):
        RuntimeError.__init__(self, message)
        self.result = result


class SpiceUnavailable(SpiceError):
    """No usable SPICE executable (not configured, not found, or not startable)."""


class SpiceTimeout(SpiceError):
    """The simulator was killed because it exceeded the timeout."""


class SpiceFailure(SpiceError):
    """The simulator exited non-zero or reported that the simulation aborted."""


class SpiceMeasureError(SpiceError):
    """A required ``.measure`` result is missing, failed, or not a finite number."""


# --------------------------------------------------------------------------- numbers

# SPICE scale factors, case-insensitive: M is milli and MEG is mega.  "mil" is 1/1000 inch.
_SCALES = (("meg", 1e6), ("mil", 25.4e-6), ("t", 1e12), ("g", 1e9), ("k", 1e3),
           ("m", 1e-3), ("u", 1e-6), ("n", 1e-9), ("p", 1e-12), ("f", 1e-15))
_NUM_RX = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)([a-z]*)\Z")
_NONFINITE_RX = re.compile(r"[+-]?(?:nan|inf(?:inity)?|1\.#[a-z]+\d*)(?:\(\w*\))?\Z", re.I)


def _to_float(text, units):
    """Value of ``text`` (may be +-inf on overflow); ValueError when it is not a number."""
    m = _NUM_RX.match(text.strip().lower())
    if not m:
        raise ValueError("not a SPICE number: %r" % text)
    mantissa, tail = m.groups()
    scale = 1.0
    for suffix, factor in _SCALES:
        if tail.startswith(suffix):
            scale, tail = factor, tail[len(suffix):]
            break
    if tail and not units:
        raise ValueError("not a SPICE number: %r" % text)
    return float(mantissa) * scale


def parse_spice_number(value, units=True):
    """Parse a SPICE number such as ``1e-9``, ``10k``, ``2.5u``, ``1MEG`` or ``3m``.

    Scale factors are case-insensitive (``M`` is milli, ``MEG`` is mega).  As in SPICE, letters
    after the scale factor are a unit and ignored (``10us``, ``5V``); pass ``units=False`` to
    forbid them.  Raises ValueError for text that is not a number and for non-finite values.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError("not a SPICE number: %r" % (value,))
    if isinstance(value, (int, float)):
        out = float(value)
    else:
        out = _to_float(str(value), units)
    if not math.isfinite(out):
        raise ValueError("non-finite SPICE number: %r" % (value,))
    return out


_MEASURE_RX = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(\S+)", re.M)


def scan_measures(text):
    """Return ``(values, invalid)`` for the ``name = value`` lines of a simulator log.

    ``values`` maps lower-case names to finite floats; ``invalid`` maps names whose result was
    ``failed`` or non-finite (nan/inf/overflow) to a short reason.  Only the first value after
    ``=`` is the result (ngspice may append ``targ=``/``trig=`` fields).  Lines whose value is
    not numeric (``Total analysis time (seconds) = ...`` has a multi-word name, notes are text)
    are ignored.  When a name repeats, the last occurrence wins.
    """
    values, invalid = {}, {}
    for m in _MEASURE_RX.finditer(str(text)):
        name, token = m.group(1).lower(), m.group(2).rstrip(",;")
        if token.lower() == "failed":
            reason = "failed"
        elif _NONFINITE_RX.match(token):
            reason = "non-finite (%s)" % token
        else:
            try:
                number = _to_float(token, units=False)
            except ValueError:
                continue
            if math.isfinite(number):
                values[name] = number
                invalid.pop(name, None)
                continue
            reason = "non-finite (%s)" % token
        invalid[name] = reason
        values.pop(name, None)
    return values, invalid


def parse_measures(text):
    """Finite ``.measure`` results from a log as ``{lower-case name: float}``.

    Failed/non-finite results are omitted here; use :func:`scan_measures` to see them.
    """
    return scan_measures(text)[0]


# --------------------------------------------------------------------------- deck

_IDENT_RX = re.compile(r"[A-Za-z_]\w*\Z")
_NODE_RX = re.compile(r"[A-Za-z_][\w.$]*\Z")
_END_RX = re.compile(r"\.end(?:\s|\Z)", re.I)
_MEASURE_ANALYSES = ("ac", "dc", "tran", "sp")
_DEFAULT_TITLE = "Digital_Layout_Auto SPICE testbench"


def _number_arg(value, name, positive=False, nonnegative=False, nonzero=False):
    """Validate a numeric deck argument and return its text (SPICE suffixes preserved)."""
    try:
        number = parse_spice_number(value)
    except ValueError:
        raise ValueError("%s must be a finite SPICE number, got %r" % (name, value))
    if positive and number <= 0:
        raise ValueError("%s must be positive, got %r" % (name, value))
    if nonnegative and number < 0:
        raise ValueError("%s must be non-negative, got %r" % (name, value))
    if nonzero and number == 0:
        raise ValueError("%s must be non-zero, got %r" % (name, value))
    if isinstance(value, str):
        return value.strip()
    return repr(number) if isinstance(value, float) else str(value)


def _path_arg(path):
    """Absolute, forward-slash, double-quotable form of ``path`` (spaces are fine)."""
    text = os.path.abspath(os.fspath(path)).replace("\\", "/")
    if '"' in text or "\n" in text or "\r" in text:
        raise ValueError("path cannot contain quotes or newlines: %r" % (text,))
    return '"%s"' % text


class SpiceDeck(object):
    """A composable SPICE testbench.

    The title line is always first, circuit statements follow in insertion order, then
    analyses, then ``.measure`` cards, and ``.end`` is emitted exactly once.  Every builder
    method validates its input before changing the deck and returns ``self`` for chaining.
    """

    def __init__(self, title=_DEFAULT_TITLE):
        self.title = title
        self._lines = []
        self._analyses = []
        self._measures = []
        self._measure_names = []

    @property
    def lines(self):
        return tuple(self._lines)

    @property
    def analyses(self):
        return tuple(self._analyses)

    @property
    def measures(self):
        return tuple(self._measures)

    @property
    def measure_names(self):
        """Lower-case names of the measurements declared with :meth:`measure`."""
        return tuple(self._measure_names)

    @staticmethod
    def _statements(lines):
        out = []
        for item in lines:
            for line in str(item).splitlines():
                line = line.strip()
                if not line:
                    continue
                if _END_RX.match(line):
                    raise ValueError(".end is emitted by the deck and cannot be added")
                out.append(line)
        if not out:
            raise ValueError("statement must be non-empty")
        return out

    def add(self, *lines):
        """Append raw circuit statements (elements, ``.subckt``, ``.param``, ``.control`` ...).

        Multi-line strings are split into statements; ``.end`` is rejected (``.ends`` and
        ``.endc`` are fine).
        """
        self._lines.extend(self._statements(lines))
        return self

    def include(self, path):
        """``.include`` an absolute-ised, quoted file path (no existence check)."""
        self._lines.append(".include %s" % _path_arg(path))
        return self

    def model(self, name, kind, parameters=None):
        """``.model name kind (parameters)``; ``parameters`` is a dict or a raw string."""
        if not _NODE_RX.match(str(name)):
            raise ValueError("invalid model name: %r" % (name,))
        if not _IDENT_RX.match(str(kind)):
            raise ValueError("invalid model kind: %r" % (kind,))
        if isinstance(parameters, dict):
            parameters = " ".join("%s=%s" % (k, v) for k, v in parameters.items())
        parameters = " ".join(str(parameters or "").split())
        text = ".model %s %s" % (name, kind)
        if parameters:
            text += " (%s)" % parameters
        self._lines.append(self._statements([text])[0])
        return self

    def op(self):
        self._analyses.append(".op")
        return self

    def dc(self, source, start, stop, step):
        if not _NODE_RX.match(str(source)):
            raise ValueError("invalid DC sweep source: %r" % (source,))
        parts = [_number_arg(start, "DC start"), _number_arg(stop, "DC stop"),
                 _number_arg(step, "DC step", nonzero=True)]
        self._analyses.append(".dc %s %s" % (source, " ".join(parts)))
        return self

    def tran(self, step, stop, start=None):
        parts = [_number_arg(step, "transient step", positive=True),
                 _number_arg(stop, "transient stop", positive=True)]
        if start is not None:
            parts.append(_number_arg(start, "transient start", nonnegative=True))
            if parse_spice_number(parts[2]) >= parse_spice_number(parts[1]):
                raise ValueError("transient start must be less than stop")
        self._analyses.append(".tran " + " ".join(parts))
        return self

    def measure(self, analysis, name, expression):
        """``.measure <analysis> <name> <expression>``; names are unique (case-insensitive)."""
        analysis = str(analysis).lower()
        if analysis not in _MEASURE_ANALYSES:
            raise ValueError("measurement analysis must be one of %s, got %r"
                             % ("/".join(_MEASURE_ANALYSES), analysis))
        if not _IDENT_RX.match(str(name)):
            raise ValueError("invalid measurement name: %r" % (name,))
        if str(name).lower() in self._measure_names:
            raise ValueError("duplicate measurement name: %r" % (name,))
        expression = " ".join(str(expression).split())
        if not expression:
            raise ValueError("measurement expression must be non-empty")
        self._measures.append(".measure %s %s %s" % (analysis, name, expression))
        self._measure_names.append(str(name).lower())
        return self

    def render(self):
        title = " ".join(str(self.title).split()) or _DEFAULT_TITLE
        return "\n".join([title] + self._lines + self._analyses + self._measures + [".end", ""])

    def write(self, path):
        """Write the deck (LF endings, UTF-8) and return its absolute path."""
        path = os.path.abspath(os.fspath(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.render())
        return path


def smoke_testbench():
    """Portable RC transient deck (tau = 1 ms) that validates an installed simulator.

    Expected: ``vfinal`` ~ 0.99988 (1 - e^-9), ``t50`` ~ 6.93e-4 (tau*ln2) and
    ``trise`` ~ 2.197e-3 (tau*ln9, 10 % -> 90 %).
    """
    return (SpiceDeck("Digital_Layout_Auto RC smoke test")
            .add("VSTEP in 0 PULSE(0 1 0 1n 1n 10m 20m)", "R1 in out 1k", "C1 out 0 1u")
            .tran("10u", "10m")
            .measure("tran", "vfinal", "FIND v(out) AT=9m")
            .measure("tran", "t50", "WHEN v(out)=0.5 RISE=1")
            .measure("tran", "trise",
                     "TRIG v(out) VAL=0.1 RISE=1 TARG v(out) VAL=0.9 RISE=1"))


# --------------------------------------------------------------------------- runner

@dataclass(frozen=True)
class SpiceResult:
    """Outcome of one run.  ``measures`` keys are lower-case; ``invalid`` holds failed/nan ones."""
    deck_path: str
    log_path: str
    command: tuple
    returncode: int
    stdout: str
    stderr: str
    log: str
    measures: dict = field(default_factory=dict)
    invalid: dict = field(default_factory=dict)

    def get(self, name, default=None):
        return self.measures.get(str(name).lower(), default)

    def require(self, name):
        """Finite value of measurement ``name`` (case-insensitive) or SpiceMeasureError."""
        key = str(name).lower()
        if key in self.measures:
            return self.measures[key]
        if key in self.invalid:
            raise SpiceMeasureError("SPICE measurement %r is %s (log: %s)"
                                    % (name, self.invalid[key], self.log_path), self)
        raise SpiceMeasureError("SPICE measurement %r is missing (log: %s)"
                                % (name, self.log_path), self)


_ABORT_RX = re.compile(r"circuit not parsed|simulation\(s\) aborted|no simulations run"
                       r"|fatal error", re.I)
_ERROR_LINE_RX = re.compile(r"^\s*(?:error|fatal)\b|\berror:|\bfatal\b", re.I)
_NAME_RX = re.compile(r'(?!\.+\Z)[^\\/:*?"<>|\x00-\x1f]+\Z')


def _split_command(command):
    """Command string -> argv tuple.  A string naming an existing file or a PATH executable
    stays one token (paths with spaces); otherwise it is split like a shell would, without
    invoking one."""
    if isinstance(command, (str, os.PathLike)):
        text = os.fspath(command).strip()
        if not text:
            raise ValueError("SPICE command is empty")
        if os.path.isfile(text) or shutil.which(text):
            return (text,)
        tokens = shlex.split(text, posix=os.name != "nt")
        if os.name == "nt":
            tokens = [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t
                      for t in tokens]
        argv = tuple(tokens)
    else:
        argv = tuple(os.fspath(x) for x in command)
    if not argv or not argv[0]:
        raise ValueError("SPICE command is empty")
    return argv


def _resolve(program):
    found = shutil.which(program)
    if found is None and os.path.isfile(program):
        found = program
    return os.path.abspath(found) if found else None  # the child starts in workdir


def _tail(text, n=3):
    lines = [x.strip() for x in str(text).splitlines() if x.strip()]
    return lines[-n:]


def _diagnostics(result):
    """Short human-readable reason pulled from stderr and the log."""
    found = []
    for text in (result.stderr, result.log, result.stdout):
        found += [x.strip() for x in text.splitlines() if _ERROR_LINE_RX.search(x)]
    found = found[:5] or _tail(result.stderr) or _tail(result.log) or _tail(result.stdout)
    return " | ".join(x[:200] for x in found) or "no simulator output"


def _text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _kill(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


class SpiceRunner(object):
    """Run ngspice in batch mode (``-b -o <log> <deck>``), never through a shell.

    Executable discovery: the explicit ``command`` (a path, or a sequence such as a wrapper
    plus arguments), else ``$DLA_SPICE``, else ``ngspice`` (then ``ngspice_con``) on ``PATH``.
    A string ``command`` that names an existing file or PATH program is one executable, so
    paths with spaces work; other strings are split shell-style.  An explicit command or
    ``$DLA_SPICE`` is never silently replaced by a PATH fallback.
    """

    def __init__(self, command=None, timeout=60):
        self.timeout = self._timeout(timeout)
        self.source = "command"
        if command is None:
            env = os.environ.get("DLA_SPICE", "").strip()
            if env:
                command, self.source = env, "$DLA_SPICE"
            else:
                self.source = "PATH"
                for exe in ("ngspice", "ngspice_con"):
                    found = shutil.which(exe)
                    if found:
                        command = found
                        break
        self.command = _split_command(command) if command is not None else ()

    @staticmethod
    def _timeout(value):
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            seconds = float("nan")
        if isinstance(value, bool) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("timeout must be a positive finite number of seconds, got %r"
                             % (value,))
        return seconds

    @property
    def executable(self):
        """Resolved path of the simulator program, or None when unavailable."""
        return _resolve(self.command[0]) if self.command else None

    @property
    def available(self):
        return self.executable is not None

    def _unavailable(self):
        if not self.command:
            return SpiceUnavailable("ngspice not found: put it on PATH, set DLA_SPICE to its "
                                    "executable, or pass command=")
        return SpiceUnavailable("SPICE executable %r (from %s) not found"
                                % (self.command[0], self.source))

    def run(self, deck, workdir=None, name=None, required=(), timeout=None):
        """Simulate ``deck`` (a :class:`SpiceDeck` or an existing deck file path).

        The deck (``<name>.sp`` when generated) and ``<name>.log`` are kept in ``workdir``
        (default: a new temporary directory).  Returns a :class:`SpiceResult`, or raises
        SpiceUnavailable, SpiceTimeout, SpiceFailure (non-zero exit / aborted simulation) or
        SpiceMeasureError (a ``required`` measurement is missing, failed or non-finite).
        """
        exe = self.executable
        if exe is None:
            raise self._unavailable()
        if isinstance(required, (str, bytes)):
            required = (required,)
        required = [str(x).lower() for x in required]
        limit = self.timeout if timeout is None else self._timeout(timeout)

        if isinstance(deck, SpiceDeck):
            deck_path = None
        else:
            deck_path = os.path.abspath(os.fspath(deck))
            if not os.path.isfile(deck_path):
                raise SpiceError("SPICE deck not found: %s" % deck_path)
        if name is None:
            name = "testbench" if deck_path is None else os.path.splitext(
                os.path.basename(deck_path))[0]
        if not _NAME_RX.match(str(name)):
            raise ValueError("name must be a plain file stem without path separators: %r"
                             % (name,))
        workdir = os.path.abspath(os.fspath(workdir)) if workdir else tempfile.mkdtemp(
            prefix="dla_spice_")
        os.makedirs(workdir, exist_ok=True)
        if deck_path is None:
            deck_path = deck.write(os.path.join(workdir, name + ".sp"))
        log_path = os.path.join(workdir, name + ".log")
        try:
            os.remove(log_path)  # a stale log must never satisfy this run's measurements
        except FileNotFoundError:
            pass

        command = (exe,) + self.command[1:] + ("-b", "-o", log_path, deck_path)
        popen_kw = {"start_new_session": True} if os.name != "nt" else {}
        try:
            proc = subprocess.Popen(command, cwd=workdir, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, encoding="utf-8",
                                    errors="replace", **popen_kw)
        except OSError as e:
            raise SpiceUnavailable("cannot start SPICE executable %r: %s" % (exe, e))
        timed_out = False
        try:
            out, err = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired as e:
            timed_out = True
            out, err = _text(e.stdout), _text(e.stderr)
            _kill(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # orphaned grandchild still holds the pipes; keep what we have

        log = ""
        if os.path.isfile(log_path):
            with open(log_path, encoding="utf-8", errors="replace") as f:
                log = f.read()
        else:  # simulator ignored -o: keep its console output as the persistent log
            log = "\n".join(x for x in (out, err) if x)
            with open(log_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(log)
        values, invalid = {}, {}
        for text in (log, out, err):  # earlier sources take precedence
            v, bad = scan_measures(text)
            for key in v:
                if key not in values and key not in invalid:
                    values[key] = v[key]
            for key in bad:
                if key not in values and key not in invalid:
                    invalid[key] = bad[key]
        result = SpiceResult(deck_path, log_path, command,
                             proc.returncode if proc.returncode is not None else -1,
                             out or "", err or "", log, values, invalid)

        if timed_out:
            raise SpiceTimeout("SPICE timed out after %gs: %s (log: %s)"
                               % (limit, deck_path, log_path), result)
        if result.returncode != 0:
            raise SpiceFailure("SPICE exited with code %d: %s (log: %s)"
                               % (result.returncode, _diagnostics(result), log_path), result)
        if any(_ABORT_RX.search(x) for x in (log, out, err)):
            raise SpiceFailure("SPICE reported an aborted simulation despite exit code 0: %s "
                               "(log: %s)" % (_diagnostics(result), log_path), result)
        problems = []
        for key in required:
            if key in invalid:
                problems.append("%s is %s" % (key, invalid[key]))
            elif key not in values:
                problems.append("%s is missing" % key)
        if problems:
            raise SpiceMeasureError("SPICE measurements unusable: %s (log: %s) [%s]"
                                    % ("; ".join(problems), log_path, _diagnostics(result)),
                                    result)
        return result
