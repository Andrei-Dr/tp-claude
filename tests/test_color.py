"""Color is decoration; it must never reach a pipe, a log or a parser."""
import os
import subprocess

import pytest


def run(script_path, env, *args):
    return subprocess.run([str(script_path), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True,
                          env={**os.environ, **env})


def test_no_escapes_when_stdout_is_not_a_tty(tmp_path, script_path):
    """subprocess.PIPE is not a terminal, so the output must be plain."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "f.txt").write_text("x\n")
    dest = tmp_path / "out"
    dest.mkdir()
    result = run(script_path, {"HOME": str(tmp_path / "home"),
                               "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")},
                 str(src), f"{dest}/")
    assert "\033[" not in result.stdout, "escape codes leaked into a pipe"


def test_no_color_env_disables_color(tpc, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(tpc.sys.stdout, "isatty", lambda: True, raising=False)
    assert tpc._color_enabled() is False


def test_dumb_terminal_disables_color(tpc, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert tpc._color_enabled() is False


def test_paint_is_identity_when_color_is_off(tpc, monkeypatch):
    monkeypatch.setattr(tpc, "COLOR", False)
    painted = tpc._paint("31")("hello")
    assert painted == "hello"


def test_paint_wraps_and_resets_when_color_is_on(tpc, monkeypatch):
    monkeypatch.setattr(tpc, "COLOR", True)
    painted = tpc._paint("31")("hello")
    assert painted == "\033[31mhello\033[0m"
    assert painted.endswith("\033[0m"), "must reset, or it bleeds into the shell"


def test_helper_program_markers_are_never_colored(script_path):
    """The tp<...>tp markers are parsed, not read. A stray escape inside one
    would break the round trip rather than just look wrong."""
    source = script_path.read_text()
    for line in source.splitlines():
        if 'print("tp<"' in line:
            assert "bold(" not in line and "dim(" not in line
            assert "warn(" not in line and "good(" not in line
