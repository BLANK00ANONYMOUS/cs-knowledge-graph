import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

import arena
import kg


def test_json_true_does_not_match_1():  # B20
    assert not arena._json_subset(True, 1)
    assert not arena._json_subset({"ok": 1}, {"ok": True})
    assert arena._json_subset({"ok": True, "n": [1, 2]}, {"ok": True, "n": [1, 2], "extra": 0})


def test_catastrophic_regex_is_time_boxed():  # B20
    t0 = time.monotonic()
    ok = asyncio.run(arena.amatches("regex", r"^(a+)+$", "a" * 40 + "!"))
    assert ok is False and time.monotonic() - t0 < arena.REGEX_TIMEOUT + 3
    assert asyncio.run(arena.amatches("regex", r"total: \d+", "x\ntotal: 42\n"))


def test_ratings_count_only_the_first_resolution(tmp_path, monkeypatch):  # B2
    monkeypatch.setattr(arena, "HISTORY", tmp_path / "history.json")
    ev = {"slug": "ch-a", "track": "backend", "difficulty": 1200}
    arena.record({"event": "solved", **ev, "wrong": 0, "hints": 0})
    once = arena.ratings()["backend"]
    arena.record({"event": "solved", **ev, "wrong": 0, "hints": 0})   # a racing second submit
    arena.record({"event": "gave-up", **ev})                         # or a give-up during judging
    assert arena.ratings()["backend"] == once


def test_corrupt_history_does_not_break_the_page(tmp_path, monkeypatch):  # B5
    monkeypatch.setattr(arena, "HISTORY", tmp_path / "history.json")
    arena.HISTORY.write_text("[{\"event\": \"solved\"", encoding="utf-8")
    assert arena.history() == []
    assert list(tmp_path.glob("history.corrupt-*.json"))


@pytest.mark.skipif(not (shutil.which("clang") or shutil.which("gcc")), reason="no C compiler")
def test_header_edit_invalidates_the_build(tmp_path, monkeypatch):  # B3
    monkeypatch.setattr(arena, "TC_CACHE", tmp_path / "tc.json")
    work, out = tmp_path / "w", tmp_path / "out"
    work.mkdir()
    (work / "v.h").write_text("#define V 1\n")
    (work / "solution.c").write_text('#include <stdio.h>\n#include "v.h"\nint main(void){printf("%d\\n", V);return 0;}\n')

    async def build_and_run():
        cmd, err = await arena.build_solution("c", work, out)
        assert cmd, err
        return (await arena.run_process(cmd, work))["stdout"].strip()
    assert asyncio.run(build_and_run()) == "1"
    (work / "v.h").write_text("#define V 2\n")
    assert asyncio.run(build_and_run()) == "2"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group containment")
def test_timeout_kills_the_whole_process_group(tmp_path):  # S4
    marker = tmp_path / "child-alive"
    script = (f"import subprocess,sys,time\n"
              f"subprocess.Popen([sys.executable,'-c','import time,pathlib;time.sleep(3);"
              f"pathlib.Path(r\"{marker}\").write_text(\"x\")'])\n"
              "time.sleep(30)\n")
    r = asyncio.run(arena.run_process([sys.executable, "-c", script], tmp_path, timeout=1))
    assert r["timeout"]
    time.sleep(3.5)
    assert not marker.exists()   # the grandchild died with its group


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimits")
def test_judged_program_cannot_write_huge_files(tmp_path):  # S4
    code = "f=open('big','wb')\nfor _ in range(100): f.write(b'x'*(1<<20))\n"
    r = asyncio.run(arena.run_process([sys.executable, "-c", code], tmp_path, timeout=10))
    assert r["exit"] != 0
    assert (tmp_path / "big").stat().st_size <= arena.FILE_LIMIT
