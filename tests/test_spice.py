"""Generic SPICE layer: deck builder, number/measure parsing, and the batch runner.

The runner is exercised against a fake ngspice (a Python script, behaviour chosen by the
FAKE_SPICE_MODE env var); one optional test runs a real ngspice when it is installed.
"""
import json
import os
import stat
import sys

import pytest

from dla.spice import (SpiceDeck, SpiceError, SpiceFailure, SpiceMeasureError, SpiceRunner,
                       SpiceTimeout, SpiceUnavailable, parse_measures, parse_spice_number,
                       scan_measures, smoke_testbench)

FAKE = r'''
import json, os, pathlib, sys, time
a = sys.argv[1:]
mode = os.environ.get("FAKE_SPICE_MODE", "ok")
log = pathlib.Path(a[a.index("-o") + 1])
deck = pathlib.Path(a[-1])
pathlib.Path(os.environ["FAKE_SPICE_RECORD"]).write_text(json.dumps(
    {"argv": a, "cwd": os.getcwd(), "deck": deck.read_text()}))
ok = "vfinal =  9.998800e-01\nT50 = 6.931e-04 targ= 1 trig= 2\n"
if mode == "ok":
    log.write_text("Circuit: x\n" + ok)
elif mode == "nomeasure":
    log.write_text("simulation complete\n")
elif mode == "stdout":
    print(ok)
elif mode == "failed":
    log.write_text("vfinal =  9.9e-01\nt50                 =  failed\n")
elif mode == "nan":
    log.write_text("vfinal = nan\nt50 = -1.#IND00\n")
elif mode == "exit2":
    log.write_text("Error: no such file models.lib\n")
    sys.stderr.write("fatal: bad deck\n")
    sys.exit(2)
elif mode == "aborted":
    log.write_text("run simulation(s) aborted\n" + ok)
elif mode == "sleep":
    time.sleep(60)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_spice.py"
    script.write_text(FAKE)
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_SPICE_RECORD", str(record))
    monkeypatch.delenv("DLA_SPICE", raising=False)

    class Fake(object):
        command = [sys.executable, str(script)]

        def mode(self, m):
            monkeypatch.setenv("FAKE_SPICE_MODE", m)
            return self

        def runner(self, **kw):
            return SpiceRunner(self.command, **kw)

        def recorded(self):
            return json.loads(record.read_text())
    return Fake()


def _wrapper(directory, script):
    """Executable named ngspice[.bat] in ``directory`` that forwards to the fake script."""
    directory.mkdir(exist_ok=True)
    if os.name == "nt":
        path = directory / "ngspice.bat"
        path.write_text('@"%s" "%s" %%*\r\n' % (sys.executable, script))
    else:
        path = directory / "ngspice"
        path.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ---- numbers and measure parsing -------------------------------------------------------

def test_spice_numbers():
    ok = {"1e-9": 1e-9, "1.5E+3": 1500, "10k": 1e4, "2.5U": 2.5e-6, "3m": 3e-3, "3M": 3e-3,
          "2MEG": 2e6, "2meg": 2e6, "1Meg": 1e6, "4n": 4e-9, "5p": 5e-12, "7f": 7e-15,
          "1T": 1e12, "1G": 1e9, "10us": 1e-5, "-.5": -0.5, 12: 12.0, 0.25: 0.25}
    for text, value in ok.items():
        assert parse_spice_number(text) == pytest.approx(value), text
    assert parse_spice_number("1x") == 1.0  # SPICE ignores trailing unit letters
    with pytest.raises(ValueError):
        parse_spice_number("10us", units=False)
    for bad in ("", "nan", "inf", "-inf", "1e999", "abc", "1%", "--1", None, True,
                float("nan"), float("inf")):
        with pytest.raises(ValueError):
            parse_spice_number(bad)


def test_nonfinite_detection_is_not_a_prefix_match():
    log = "info = information\nmodel = infeasible\nx = inf\ny = -nan(ind)\nz = 1.#QNAN\nw = Infinity"
    values, invalid = scan_measures(log)
    assert values == {} and set(invalid) == {"x", "y", "z", "w"}


def test_parse_measures_formats_and_noise():
    log = "\n".join([
        "Circuit: RC",
        "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000",
        "Total analysis time (seconds) = 0.02",
        "vfinal              =  9.998800e-01",
        "  T50 = 693.1u targ= 7.0e-04 trig= 1.0e-05",
        "MEGv = 1MEG",
        "milli = 2M",
        "dup = 1",
        "dup = 2",
        "bad = failed",
        "n1 = nan",
        "n2 = -inf",
        "n3 = 1.#INF",
        "n4 = 1e999",
        "note = hello",
    ])
    assert parse_measures(log) == pytest.approx({
        "vfinal": 0.99988, "t50": 6.931e-4, "megv": 1e6, "milli": 2e-3, "dup": 2.0})


# ---- deck ------------------------------------------------------------------------------

def test_deck_renders_in_order_with_single_end(tmp_path):
    lib = tmp_path / "my models" / "m.lib"
    d = (SpiceDeck("rc\ndivider")
         .include(lib)
         .model("nsimple", "nmos", {"level": 1, "vto": 0.4})
         .add("V1 in 0 1", "R1 in out 1k\nR2 out 0 1k", ".ends")
         .op()
         .dc("V1", 0, 1, 0.1)
         .tran("1n", "10u", "1u")
         .measure("DC", "Half", "FIND v(out) AT=1"))
    lines = d.render().split("\n")
    assert lines[0] == "rc divider"
    assert lines[1] == '.include "%s"' % str(lib).replace("\\", "/")
    assert ".model nsimple nmos (level=1 vto=0.4)" in lines
    assert lines[3:7] == ["V1 in 0 1", "R1 in out 1k", "R2 out 0 1k", ".ends"]
    assert lines[-6:] == [".op", ".dc V1 0 1 0.1", ".tran 1n 10u 1u",
                          ".measure dc Half FIND v(out) AT=1", ".end", ""]
    assert d.render().lower().count(".end\n") == 1
    assert d.measure_names == ("half",)
    path = d.write(tmp_path / "new dir" / "tb.sp")
    assert path == str(tmp_path / "new dir" / "tb.sp")
    assert open(path, newline="").read() == d.render()


def test_deck_write_bare_filename_and_relative_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = smoke_testbench()
    assert d.write("bare.sp") == str(tmp_path / "bare.sp")
    assert (tmp_path / "bare.sp").read_text() == d.render()
    assert d.write(os.path.join("rel", "tb.sp")) == str(tmp_path / "rel" / "tb.sp")


def test_deck_rejects_bad_input_atomically():
    d = SpiceDeck().add("R1 a b 1k")
    for bad in ([""], ["R2 a b 1k\n.END"], ["  .end  "], ["R3 a b 1k", ".end now"]):
        with pytest.raises(ValueError):
            d.add(*bad)
    assert d.lines == ("R1 a b 1k",)
    with pytest.raises(ValueError):
        d.include('bad"quote.lib')
    with pytest.raises(ValueError):
        d.model("bad name", "nmos")
    with pytest.raises(ValueError):
        d.dc("V1", 0, 1, 0)
    for args in (("0", "1u"), ("1n", "0"), ("1n", "nan"), ("1n", "1u", "-1"), ("1n", "1u", "2u")):
        with pytest.raises(ValueError):
            d.tran(*args)
    d.measure("tran", "m1", "FIND v(a) AT=1n")
    for args in (("op", "x", "v(a)"), ("tran", "M1", "v(a)"), ("tran", "1x", "v(a)"),
                 ("tran", "x", "  ")):
        with pytest.raises(ValueError):
            d.measure(*args)
    assert d.analyses == () and d.measure_names == ("m1",)


def test_smoke_testbench_syntax():
    text = smoke_testbench().render()
    assert text.split("\n")[1:] == [
        "VSTEP in 0 PULSE(0 1 0 1n 1n 10m 20m)", "R1 in out 1k", "C1 out 0 1u",
        ".tran 10u 10m",
        ".measure tran vfinal FIND v(out) AT=9m",
        ".measure tran t50 WHEN v(out)=0.5 RISE=1",
        ".measure tran trise TRIG v(out) VAL=0.1 RISE=1 TARG v(out) VAL=0.9 RISE=1",
        ".end", ""]


# ---- runner ----------------------------------------------------------------------------

def test_run_persists_deck_and_log_and_parses(fake, tmp_path):
    work = tmp_path / "work dir"  # spaces in the working directory
    deck = smoke_testbench()
    result = fake.mode("ok").runner().run(deck, work, "smoke", required=deck.measure_names[:2])
    assert result.require("VFINAL") == pytest.approx(0.99988)
    assert result.require("t50") == pytest.approx(6.931e-4)
    assert result.get("missing", -1) == -1
    assert os.path.isfile(result.deck_path) and result.deck_path == str(work / "smoke.sp")
    assert result.log_path == str(work / "smoke.log")
    assert "Circuit: x" in open(result.log_path).read() and result.log == open(result.log_path).read()
    rec = fake.recorded()
    assert rec["argv"] == ["-b", "-o", result.log_path, result.deck_path]
    assert os.path.samefile(rec["cwd"], str(work)) and rec["deck"] == deck.render()
    with pytest.raises(SpiceMeasureError, match="'nothere' is missing"):
        result.require("nothere")


def test_run_existing_deck_file_and_default_workdir(fake, tmp_path):
    deck = tmp_path / "my deck.sp"
    deck.write_text("title\n.end\n")
    result = fake.mode("ok").runner().run(deck)
    assert result.deck_path == str(deck)
    assert os.path.basename(result.log_path) == "my deck.log"
    assert os.path.isfile(result.log_path)
    with pytest.raises(SpiceError, match="not found"):
        fake.runner().run(tmp_path / "nope.sp", tmp_path)
    with pytest.raises(ValueError):
        fake.runner().run(smoke_testbench(), tmp_path, name="../evil")
    with pytest.raises(ValueError):
        fake.runner().run(smoke_testbench(), tmp_path, name="..")


def test_measures_from_stdout_when_log_not_written(fake, tmp_path):
    result = fake.mode("stdout").runner().run(smoke_testbench(), tmp_path, required="t50")
    assert result.require("t50") == pytest.approx(6.931e-4)
    assert "vfinal" in open(result.log_path).read()  # console output kept as the log


def test_stale_log_is_not_reused(fake, tmp_path):
    runner = fake.runner()
    runner.run(smoke_testbench(), tmp_path, "tb", required=["t50"])
    with pytest.raises(SpiceMeasureError, match="t50 is missing"):
        fake.mode("nomeasure").runner().run(smoke_testbench(), tmp_path, "tb", required=["t50"])


def test_nonzero_exit_reports_stderr_and_log(fake, tmp_path):
    with pytest.raises(SpiceFailure, match="exit(ed)? .*code 2") as exc:
        fake.mode("exit2").runner().run(smoke_testbench(), tmp_path)
    assert "fatal: bad deck" in str(exc.value) and "no such file" in str(exc.value)
    assert exc.value.result.returncode == 2 and "fatal: bad deck" in exc.value.result.stderr


def test_exit_zero_but_aborted(fake, tmp_path):
    with pytest.raises(SpiceFailure, match="aborted"):
        fake.mode("aborted").runner().run(smoke_testbench(), tmp_path)


def test_missing_failed_and_nonfinite_required_measures(fake, tmp_path):
    with pytest.raises(SpiceMeasureError, match="vfinal is missing; t50 is missing"):
        fake.mode("nomeasure").runner().run(smoke_testbench(), tmp_path,
                                           required=["VFinal", "T50"])
    with pytest.raises(SpiceMeasureError, match="t50 is failed"):
        fake.mode("failed").runner().run(smoke_testbench(), tmp_path, required=["vfinal", "t50"])
    result = fake.mode("failed").runner().run(smoke_testbench(), tmp_path)  # not required: ok
    assert result.invalid == {"t50": "failed"} and result.measures == pytest.approx({"vfinal": 0.99})
    with pytest.raises(SpiceMeasureError, match="vfinal is non-finite"):
        fake.mode("nan").runner().run(smoke_testbench(), tmp_path, required=["vfinal"])
    result = fake.mode("nan").runner().run(smoke_testbench(), tmp_path)
    with pytest.raises(SpiceMeasureError, match="non-finite"):
        result.require("t50")


def test_timeout_kills_simulator(fake, tmp_path):
    with pytest.raises(SpiceTimeout, match="timed out") as exc:
        fake.mode("sleep").runner(timeout=0.5).run(smoke_testbench(), tmp_path)
    assert os.path.isfile(exc.value.result.log_path)
    for bad in (0, -1, float("nan"), "x", True):
        with pytest.raises(ValueError):
            SpiceRunner(fake.command, timeout=bad)


def test_unavailable(fake, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("DLA_SPICE", raising=False)
    runner = SpiceRunner()
    assert runner.command == () and not runner.available
    with pytest.raises(SpiceUnavailable, match="ngspice not found"):
        runner.run(smoke_testbench(), tmp_path)
    with pytest.raises(SpiceUnavailable, match="not found"):
        SpiceRunner("definitely-not-a-simulator-xyz").run(smoke_testbench(), tmp_path)
    monkeypatch.setenv("DLA_SPICE", str(tmp_path / "missing" / "ngspice"))
    with pytest.raises(SpiceUnavailable, match="DLA_SPICE"):  # no silent PATH fallback
        SpiceRunner().run(smoke_testbench(), tmp_path)
    with pytest.raises(ValueError):
        SpiceRunner([])


def test_relative_command_and_workdir(fake, tmp_path, monkeypatch):
    """The child starts in workdir, so a relative simulator path must be made absolute."""
    wrapper = _wrapper(tmp_path / "bin", tmp_path / "fake_spice.py")
    monkeypatch.chdir(tmp_path)
    fake.mode("ok")
    runner = SpiceRunner(os.path.join("bin", wrapper.name))
    assert os.path.isabs(runner.executable)
    result = runner.run(smoke_testbench(), "rel_work", required=["t50"])
    assert result.deck_path == str(tmp_path / "rel_work" / "testbench.sp")
    assert result.require("t50") == pytest.approx(6.931e-4)


def test_discovery_order_and_paths_with_spaces(fake, tmp_path, monkeypatch):
    script = tmp_path / "fake_spice.py"
    path_bin = _wrapper(tmp_path / "pathbin", script)
    env_bin = _wrapper(tmp_path / "env bin", script)  # executable path containing a space
    monkeypatch.setenv("PATH", str(path_bin.parent) + os.pathsep + os.environ["PATH"])
    fake.mode("ok")

    assert os.path.normcase(SpiceRunner().executable) == os.path.normcase(str(path_bin))  # PATH
    monkeypatch.setenv("DLA_SPICE", str(env_bin))
    env_runner = SpiceRunner()
    assert env_runner.command == (str(env_bin),)  # $DLA_SPICE beats PATH, kept as one token
    explicit = SpiceRunner(fake.command)
    assert explicit.command == tuple(fake.command)  # explicit beats $DLA_SPICE

    result = env_runner.run(smoke_testbench(), tmp_path / "out dir", required=["t50"])
    assert result.require("t50") == pytest.approx(6.931e-4)
    assert SpiceRunner("%s -x" % path_bin).command == (str(path_bin), "-x")  # shell-style split


@pytest.mark.skipif(not SpiceRunner().available, reason="ngspice not installed")
def test_real_ngspice_smoke(tmp_path):
    deck = smoke_testbench()
    result = SpiceRunner().run(deck, tmp_path, "real", required=deck.measure_names)
    assert result.require("vfinal") == pytest.approx(0.99988, abs=2e-4)
    assert result.require("t50") == pytest.approx(6.931e-4, rel=0.02)
    assert result.require("trise") == pytest.approx(2.197e-3, rel=0.02)
