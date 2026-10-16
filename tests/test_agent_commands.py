"""Finding the zerostore program for the lines people paste into their agents."""

from __future__ import annotations

import os
import stat
import sys

from zerostore.agents import commands


def _program(folder, name="zerostore"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_the_program_next_to_this_python_wins(tmp_path, monkeypatch):
    venv = tmp_path / "tools" / "zerostore" / "bin"
    monkeypatch.setattr(sys, "executable", str(venv / "python"))
    _program(venv)
    _program(tmp_path / "elsewhere")
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    assert commands.program() == str(venv / "zerostore")


def test_otherwise_the_one_on_path_then_the_bare_name(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python" / "bin" / "python3"))
    _program(tmp_path / "python" / "bin", "not-zerostore")
    found = _program(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert commands.program() == str(found)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert commands.program() == "zerostore"


def test_a_file_that_cannot_run_is_skipped(tmp_path, monkeypatch):
    venv = tmp_path / "bin"
    monkeypatch.setattr(sys, "executable", str(venv / "python"))
    venv.mkdir()
    (venv / "zerostore").write_text("not a program")
    os.chmod(venv / "zerostore", 0o644)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert commands.program() == "zerostore"
