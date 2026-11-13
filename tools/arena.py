"""Development challenges arena: AI-set practical problems, a local judge, and per-track ratings.

A challenge is a small realistic engineering task (CLI tool, parser, stateful store, REST API...). Copilot writes
the statement, starter code, tests and a hidden Python reference solution; the tests are validated by running the
reference before the challenge is published. The learner solves it in Python, C or C++ and the judge runs the
public + hidden tests (Codeforces-style verdicts), then Copilot reviews the code.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import kg
from builder import catalog, learner_profile, notes as load_notes, update_fields, write_note
from tutor import TutorError

WORK_DIR = kg.ROOT / "challenges"            # learner-visible: statements, starter code, public tests, solutions
SECRET_DIR = kg.ROOT / ".kg" / "challenges"  # gitignored: full spec, hidden tests, reference solution, builds
HISTORY = WORK_DIR / "history.json"
TRACKS = ["backend", "systems", "security", "ai", "tooling"]
TRACK_DOMAIN = {"backend": "cs", "systems": "systems", "security": "security", "ai": "ai", "tooling": "cs"}
LANGS = {"python": "solution.py", "c": "solution.c", "cpp": "solution.cpp"}
LANG_LABEL = {"python": "Python", "c": "C", "cpp": "C++"}
BASE_RATING = 1200
OUT_LIMIT = 256 * 1024

# Best-effort guard for the AI-written reference solution, which runs on this machine during validation.
_BANNED_MODULES = r"(subprocess|shutil|ctypes|multiprocessing|requests|ftplib|smtplib|telnetlib|paramiko|webbrowser|urllib\.request|http\.client)"
UNSAFE_REFERENCE = [
    rf"(?m)^\s*import\s+[^\n#]*\b{_BANNED_MODULES}\b",
    rf"(?m)^\s*from\s+{_BANNED_MODULES}\b",
    r"(?m)^\s*from\s+urllib\s+import\s+[^\n#]*\brequest\b",
    r"\bos\.(system|popen|remove|unlink|rmdir|removedirs|rename|replace|chmod|kill|exec\w*|spawn\w*|fork)\s*\(",
    r"(?<![\w.])(eval|exec|__import__|compile)\s*\(",
    r"open\(\s*[rbf]?['\"](/|\\|[A-Za-z]:)",
]


def unsafe_reason(code: str) -> str:
    for p in UNSAFE_REFERENCE:
        m = re.search(p, code)
        if m:
            return m.group(0).strip()[:80]
    return ""


# --------------------------------------------------------------------------- schemas

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileSpec(Strict):
    path: str = Field(description="relative path created in the working directory before the run")
    content: str


class TestCase(Strict):
    name: str = Field(description="short description of what this test checks")
    hidden: bool = Field(description="false = shown to the learner as an example; true = hidden edge case")
    args: list[str] = Field(default_factory=list, description="cli: command-line arguments")
    stdin: str = Field(default="", description="cli: standard input")
    files: list[FileSpec] = Field(default_factory=list, description="cli: input files in the working directory")
    method: str = Field(default="GET", description="http: request method")
    path: str = Field(default="/", description="http: request path incl. query string")
    body: str = Field(default="", description="http: request body")
    expect_status: int = Field(default=200, description="http: expected status code")
    expect: str = Field(description="expected stdout (cli) or response body (http)")
    match: Literal["exact", "lines", "contains", "regex", "json"] = Field(
        default="exact", description="exact (whitespace-normalised) | lines (unordered lines) | contains | regex | "
                                     "json (expected JSON is a subset of the actual JSON)")
    expect_exit: int = Field(default=0, description="cli: expected exit code (-1 = any)")


class ChallengeSpec(Strict):
    title: str
    track: Literal["backend", "systems", "security", "ai", "tooling"]
    kind: Literal["cli", "http"]
    difficulty: int = Field(description="rating 800-2400")
    est_minutes: int
    story: str = Field(description="2-3 sentence realistic scenario")
    statement: str = Field(description="markdown: numbered requirements")
    interface: str = Field(description="markdown: the exact input/output contract incl. error messages and exit codes")
    constraints: list[str]
    concepts: list[str] = Field(description="concept slugs from the catalog this exercises")
    hints: list[str] = Field(description="3 progressive hints, vague to specific, never full code")
    editorial: str = Field(description="markdown: approach, key design decisions, pitfalls (no full code)")
    starter: str = Field(description="starter code in the learner's language: runs/compiles, reads input, TODOs, "
                                     "does NOT solve the task")
    reference_python: str = Field(description="complete Python 3 standard-library reference solution")
    tests: list[TestCase]


class Repair(Strict):
    reference_python: str
    tests: list[TestCase]


class RubricScore(Strict):
    area: Literal["correctness", "design", "error handling", "readability", "testing", "security", "performance"]
    score: int = Field(description="1-5")
    comment: str


class Review(Strict):
    summary: str
    scores: list[RubricScore]
    strengths: list[str]
    improvements: list[str] = Field(description="top 3 concrete improvements, most important first")
    next_step: str = Field(description="what to practise next")


ARENA_SYSTEM = """You are a problem setter and senior code reviewer for a practical SOFTWARE-DEVELOPMENT judge — like
Codeforces, but for building real software rather than solving algorithm puzzles. Problems are small, realistic
engineering tasks: CLI tools, parsers for real formats (logs, CSV, INI, HTTP requests, URLs), stateful stores and
command interpreters (key-value store, rate limiter, LRU cache with TTL, job queue), REST APIs, config/input
validators, file processing, simple protocols, security tasks (safe parsing, path-traversal checks, password
policy, log anomaly detection), systems tasks (memory-pool or scheduler simulations, ring buffers, binary formats).
Algorithms appear only as part of a realistic feature. Specs must be precise, deterministic and testable."""


# --------------------------------------------------------------------------- toolchain

_TOOLCHAIN: dict | None = None


def toolchains() -> dict:
    """Find working C/C++ compilers by actually compiling a probe (some installs are present but broken)."""
    global _TOOLCHAIN
    if _TOOLCHAIN is not None:
        return _TOOLCHAIN
    import subprocess
    found: dict = {"python": PYTHON}
    probes = {"c": (["clang", "gcc", "cc"], "int main(void){return 0;}", ".c"),
              "cpp": (["clang++", "g++", "c++"], "int main(){return 0;}", ".cpp")}
    with tempfile.TemporaryDirectory() as tmp:
        for lang, (names, code, ext) in probes.items():
            src = Path(tmp) / f"probe{ext}"
            src.write_text(code)
            for name in names:
                exe = shutil.which(name)
                if not exe:
                    continue
                out = Path(tmp) / f"probe-{name}.exe"
                try:
                    r = subprocess.run([exe, "-static", "-o", str(out), str(src)], capture_output=True, timeout=60)
                except (OSError, subprocess.SubprocessError):
                    continue
                if r.returncode == 0 and out.exists():
                    found[lang] = exe
                    break
    _TOOLCHAIN = found
    return found


def judge_env() -> dict:
    """Minimal environment for judged programs: no tokens or secrets."""
    keep = ["PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT", "LANG"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


# --------------------------------------------------------------------------- process running

PYTHON = getattr(sys, "_base_executable", None) or sys.executable  # venv python.exe on Windows is a launcher stub


async def _kill_tree(p) -> None:
    """Kill a judged process and its children (Windows needs taskkill /T for that)."""
    if p.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec("taskkill", "/F", "/T", "/PID", str(p.pid),
                                                      stdout=asyncio.subprocess.DEVNULL,
                                                      stderr=asyncio.subprocess.DEVNULL)
        await killer.wait()
    if p.returncode is None:
        try:
            p.kill()
        except ProcessLookupError:
            pass
    await p.wait()


async def _drain(stream, limit: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(buf)
        if len(buf) < limit:
            buf += chunk[: limit - len(buf)]


async def run_process(cmd: list[str], cwd: Path, stdin: str = "", timeout: float = 5.0) -> dict:
    try:
        p = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), env=judge_env(), stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        return {"exit": None, "stdout": "", "stderr": str(exc), "timeout": False, "ms": 0}
    start = asyncio.get_running_loop().time()

    async def feed():
        try:
            if stdin:
                p.stdin.write(stdin.encode())
                await p.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass

    try:
        _, out, err, code = await asyncio.wait_for(
            asyncio.gather(feed(), _drain(p.stdout, OUT_LIMIT), _drain(p.stderr, 16 * 1024), p.wait()), timeout)
        timed_out = False
    except asyncio.TimeoutError:
        await _kill_tree(p)
        out, err, code, timed_out = b"", b"", None, True
    ms = int((asyncio.get_running_loop().time() - start) * 1000)
    return {"exit": code, "stdout": out.decode("utf-8", "replace"), "stderr": err.decode("utf-8", "replace"),
            "timeout": timed_out, "ms": ms}


def _norm(s: str) -> str:
    return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip("\n")


def _json_subset(expected, actual) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _json_subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            _json_subset(e, a) for e, a in zip(expected, actual))
    return expected == actual


def matches(mode: str, expected: str, actual: str) -> bool:
    e, a = _norm(expected), _norm(actual)
    if mode == "exact":
        return e == a
    if mode == "lines":
        return sorted(x for x in e.split("\n") if x) == sorted(x for x in a.split("\n") if x)
    if mode == "contains":
        return e in a
    if mode == "regex":
        try:
            return re.search(expected, actual.replace("\r\n", "\n"), re.M | re.S) is not None
        except re.error:
            return False
    if mode == "json":
        try:
            return _json_subset(json.loads(expected), json.loads(actual))
        except (ValueError, TypeError):
            return False
    return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _setup_files(work: Path, files: list[FileSpec]) -> None:
    for f in files:
        target = (work / f.path).resolve()
        if work.resolve() not in target.parents:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8")


async def run_cli_test(cmd: list[str], t: TestCase, timeout: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="kg-judge-") as tmp:
        work = Path(tmp)
        _setup_files(work, t.files)
        r = await run_process(cmd + t.args, work, t.stdin, timeout)
    if r["timeout"]:
        verdict = "TLE"
    elif r["exit"] is None:
        verdict = "RE"
    elif t.expect_exit != -1 and r["exit"] != t.expect_exit:
        verdict = "RE" if t.expect_exit == 0 else "WA"
    elif not matches(t.match, t.expect, r["stdout"]):
        verdict = "WA"
    else:
        verdict = "OK"
    return {"verdict": verdict, "ms": r["ms"], "exit": r["exit"], "stdout": r["stdout"][:4000],
            "stderr": r["stderr"][:2000]}


async def run_http_suite(cmd: list[str], tests: list[TestCase], timeout: float) -> list[dict]:
    import aiohttp
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="kg-judge-") as tmp:
        work = Path(tmp)
        env = {**judge_env(), "PORT": str(port)}
        errfile = open(work / ".server-stderr.txt", "wb")
        try:
            p = await asyncio.create_subprocess_exec(*cmd, str(port), cwd=str(work), env=env,
                                                     stdin=asyncio.subprocess.DEVNULL,
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=errfile)
        except OSError as exc:
            errfile.close()
            return [{"verdict": "RE", "ms": 0, "error": str(exc)} for _ in tests]
        try:
            ready = False
            for _ in range(100):
                if p.returncode is not None:
                    break
                try:
                    _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.5)
                    w.close()
                    ready = True
                    break
                except (OSError, asyncio.TimeoutError):
                    await asyncio.sleep(0.1)
            if not ready:
                errfile.flush()
                err = (work / ".server-stderr.txt").read_text(encoding="utf-8", errors="replace")[-2000:]
                msg = f"server did not start listening on port {port}" + (f": {err}" if err else "")
                return [{"verdict": "RE", "ms": 0, "error": msg} for _ in tests]
            results = []
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
                for t in tests:
                    start = asyncio.get_running_loop().time()
                    try:
                        async with http.request(t.method, f"http://127.0.0.1:{port}{t.path}",
                                                data=t.body.encode() if t.body else None,
                                                headers={"Content-Type": "application/json"} if t.body else None) as r:
                            text = await r.text(errors="replace")
                            status = r.status
                        ok = status == t.expect_status and matches(t.match, t.expect, text)
                        results.append({"verdict": "OK" if ok else "WA", "status": status, "stdout": text[:4000],
                                        "ms": int((asyncio.get_running_loop().time() - start) * 1000)})
                    except asyncio.TimeoutError:
                        results.append({"verdict": "TLE", "ms": int(timeout * 1000)})
                    except aiohttp.ClientError as exc:
                        results.append({"verdict": "RE", "ms": 0, "error": f"{type(exc).__name__}: {exc}"})
            return results
        finally:
            await _kill_tree(p)
            errfile.close()


async def run_tests(cmd: list[str], kind: str, tests: list[TestCase], timeout: float = 5.0) -> list[dict]:
    if kind == "http":
        out = []
        for hidden in (False, True):   # each suite gets a fresh server; tests run in order
            suite = [t for t in tests if t.hidden == hidden]
            if suite:
                out += list(zip(suite, await run_http_suite(cmd, suite, timeout)))
        order = {id(t): i for i, t in enumerate(tests)}
        return [r for _, r in sorted(out, key=lambda x: order[id(x[0])])]
    sem = asyncio.Semaphore(4)

    async def one(t):
        async with sem:
            return await run_cli_test(cmd, t, timeout)
    return await asyncio.gather(*(one(t) for t in tests))


# --------------------------------------------------------------------------- storage

def _spec_path(slug: str) -> Path:
    return SECRET_DIR / slug / "spec.json"


def load_spec(slug: str) -> tuple[ChallengeSpec, dict]:
    p = _spec_path(slug)
    if not p.exists():
        raise TutorError("this challenge's hidden tests are missing (they live in .kg/ on the machine that created "
                         "it); generate a new challenge", 404)
    data = json.loads(p.read_text(encoding="utf-8"))
    return ChallengeSpec.model_validate(data["spec"]), data["meta"]


def history() -> list[dict]:
    return json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []


def record(event: dict) -> None:
    h = history()
    h.append({"t": dt.datetime.now().isoformat(timespec="seconds"), **event})
    WORK_DIR.mkdir(exist_ok=True)
    HISTORY.write_text(json.dumps(h, indent=1, ensure_ascii=False), encoding="utf-8")


def ratings() -> dict:
    """Elo-style rating per track, recomputed from history. Only the first resolution of a challenge counts:
    Accepted scores 1 minus penalties for wrong submissions and hints; giving up scores 0."""
    r = {t: {"rating": BASE_RATING, "solved": 0, "attempted": 0, "peak": BASE_RATING} for t in TRACKS}
    for e in history():
        if e.get("event") not in ("solved", "gave-up") or e.get("track") not in r:
            continue
        s = r[e["track"]]
        expected = 1 / (1 + 10 ** ((e["difficulty"] - s["rating"]) / 400))
        score = 0.0 if e["event"] == "gave-up" else max(0.5, 1 - 0.1 * e.get("wrong", 0) - 0.05 * e.get("hints", 0))
        k = 48 if s["attempted"] < 5 else 32
        delta = round(k * (score - expected))
        e["delta"] = delta
        s["rating"] += delta
        s["attempted"] += 1
        s["solved"] += e["event"] == "solved"
        s["peak"] = max(s["peak"], s["rating"])
    return r


def suggested_difficulty(rating: int) -> int:
    return max(800, min(2400, round(rating / 100) * 100 + 100))


def title_for(r: int) -> str:
    for limit, name in ((1200, "Newbie"), (1400, "Pupil"), (1600, "Specialist"), (1900, "Expert"),
                        (2100, "Candidate Master"), (2400, "Master")):
        if r < limit:
            return name
    return "Grandmaster"


# --------------------------------------------------------------------------- engine

class Arena:
    def __init__(self, tutor):
        self.tutor = tutor
        self.locks: dict[str, asyncio.Lock] = {}

    def _lock(self, slug: str) -> asyncio.Lock:
        return self.locks.setdefault(slug, asyncio.Lock())

    # ---- overview

    def overview(self) -> dict:
        n = load_notes()
        rt = ratings()
        items = []
        for s, x in n.items():
            if x.type != "challenge":
                continue
            items.append({"slug": s, "title": x.title, "track": x.meta.get("track"), "language": x.meta.get("language"),
                          "difficulty": x.meta.get("difficulty"), "status": x.meta.get("status", "open"),
                          "kind": x.meta.get("kind"), "attempts": x.meta.get("attempts", 0),
                          "created": str(x.meta.get("created") or ""), "solved_on": str(x.meta.get("solved_on") or ""),
                          "est_minutes": x.meta.get("est_minutes"), "practices": x.refs("practices"),
                          "secret": _spec_path(s).exists()})
        items.sort(key=lambda c: c["created"], reverse=True)
        tc = toolchains()
        return {
            "tracks": [{"track": t, **rt[t], "title": title_for(rt[t]["rating"]),
                        "suggested": suggested_difficulty(rt[t]["rating"])} for t in TRACKS],
            "challenges": items,
            "languages": [{"id": k, "label": LANG_LABEL[k], "available": k in tc} for k in LANGS],
            "concepts": sorted(({"slug": s, "title": x.title, "domain": x.domain} for s, x in n.items()
                                if x.type == "concept"), key=lambda c: c["title"]),
            "recent": [e for e in reversed(history()) if e.get("event") in ("submit", "solved", "gave-up")][:15],
        }

    def detail(self, slug: str) -> dict:
        n = load_notes()
        note = n.get(slug)
        if not note or note.type != "challenge":
            raise TutorError("unknown challenge", 404)
        spec, meta = load_spec(slug)
        status = note.meta.get("status", "open")
        hints_used = int(note.meta.get("hints_used") or 0)
        rel = (WORK_DIR / slug).relative_to(kg.ROOT).as_posix()
        out = {
            "slug": slug, "title": spec.title, "track": spec.track, "kind": spec.kind, "difficulty": spec.difficulty,
            "est_minutes": spec.est_minutes, "language": meta["language"], "status": status,
            "attempts": note.meta.get("attempts", 0), "story": spec.story, "statement": spec.statement,
            "interface": spec.interface, "constraints": spec.constraints,
            "concepts": [{"slug": c, "title": n[c].title} for c in spec.concepts if c in n],
            "examples": [t.model_dump() for t in spec.tests if not t.hidden],
            "hidden_count": sum(t.hidden for t in spec.tests),
            "hints": spec.hints[:hints_used], "hints_left": max(0, len(spec.hints) - hints_used),
            "folder": rel, "folder_abs": str(WORK_DIR / slug), "entry": f"{rel}/{LANGS[meta['language']]}",
            "submissions": [e for e in history() if e.get("slug") == slug and e.get("event") in ("submit", "run")][-10:],
        }
        if status in ("solved", "gave-up"):
            out["editorial"] = spec.editorial
            out["reference"] = spec.reference_python
        return out

    # ---- generation

    async def generate(self, track: str, language: str, difficulty: int | None, concept: str | None,
                       theme: str) -> dict:
        if track not in TRACKS:
            raise TutorError(f"track must be one of {TRACKS}")
        if language not in LANGS:
            raise TutorError("language must be python, c or cpp")
        if language not in toolchains():
            raise TutorError(f"no working {LANG_LABEL[language]} compiler found on this machine")
        n = load_notes()
        if concept and (concept not in n or n[concept].type != "concept"):
            raise TutorError(f"unknown concept '{concept}'")
        rating = ratings()[track]["rating"]
        difficulty = max(800, min(2400, int(difficulty or suggested_difficulty(rating))))
        existing = [x.title for x in n.values() if x.type == "challenge"]
        prompt = "\n".join([
            f"Write ONE new challenge. Track: {track}. Learner's language: {LANG_LABEL[language]}. "
            f"Target difficulty: {difficulty} (learner's {track} rating: {rating}).",
            f"Focus concept: {n[concept].title} ({concept})." if concept else "",
            f"Extra theme/interests: {theme}" if theme else "",
            f"Avoid repeating these existing challenges: {'; '.join(existing[-30:])}" if existing else "",
            "", f"Learner profile:\n{learner_profile(n)}", "", f"Concept catalog:\n{catalog(n)}", "",
            "Rules:",
            "- kind 'cli': the program reads stdin, command-line args and/or files in its working directory, "
            "writes stdout and exits with a code. kind 'http': the program takes the port as its FIRST command-line "
            "argument and serves HTTP on 127.0.0.1:<port>; tests run IN ORDER against one fresh server (public "
            "tests and hidden tests are separate suites, each on a fresh server, so each suite must be "
            "self-contained). Use kind 'http' only when the learner's language is Python.",
            "- Solvable with the language's standard library only. Deterministic output (no timestamps/randomness; "
            "if line order is unspecified use match 'lines'). Keep outputs short.",
            "- tests: 3-5 public examples (hidden=false) and 6-10 hidden tests (hidden=true) covering edge cases, "
            "invalid input and error handling. Expected outputs must follow the interface EXACTLY.",
            "- reference_python: complete, correct Python 3 standard-library solution. No network except serving "
            "the HTTP port, no subprocess/eval/exec, never delete files or use absolute paths.",
            "- starter: in " + LANG_LABEL[language] + "; must run/compile, show the program skeleton (I/O, arg "
            "parsing) with TODOs, and must NOT solve the task.",
            "- Difficulty guide: 800 ≈ 30 lines, few requirements; 1200 ≈ 80 lines with careful edge cases; "
            "1600 ≈ several components or state; 2000+ ≈ substantial design.",
        ])
        last_error = ""
        for attempt in range(2):
            spec = await self.tutor.ask_typed(ARENA_SYSTEM, prompt + last_error, ChallengeSpec, timeout=480)
            spec.difficulty = difficulty
            spec.track = track
            if language != "python":
                spec.kind = "cli"
            spec.concepts = [c for c in dict.fromkeys(spec.concepts) if c in n and n[c].type == "concept"][:5]
            if concept and concept not in spec.concepts:
                spec.concepts.insert(0, concept)
            reason = unsafe_reason(spec.reference_python)
            if reason:
                last_error = (f"\n\nThe previous reference solution used a forbidden API ({reason!r}); use only safe "
                              "standard-library code.")
                continue
            spec, dropped = await self._validate(spec)
            if spec:
                return self._publish(spec, language, dropped)
            last_error = "\n\nThe previous attempt's tests and reference solution did not agree; be precise."
        raise TutorError("couldn't produce a challenge whose tests verify against its reference solution; "
                         "please try again", 502)

    async def _validate(self, spec: ChallengeSpec) -> tuple[ChallengeSpec | None, int]:
        for round_ in range(2):
            with tempfile.TemporaryDirectory(prefix="kg-ref-") as tmp:
                ref = Path(tmp) / "reference.py"
                ref.write_text(spec.reference_python, encoding="utf-8")
                results = await run_tests([PYTHON, str(ref)], spec.kind, spec.tests, timeout=8)
            failed = [(t, r) for t, r in zip(spec.tests, results) if r["verdict"] != "OK"]
            if not failed:
                return spec, 0
            if round_ == 0:
                detail = "\n".join(
                    f"- test '{t.name}': verdict {r['verdict']}; expected ({t.match}): {t.expect[:400]!r}; "
                    f"actual output: {r.get('stdout', '')[:400]!r}; exit {r.get('exit')}; "
                    f"status {r.get('status')}; stderr/error: {(r.get('stderr') or r.get('error') or '')[:300]!r}"
                    for t, r in failed[:8])
                fix = await self.tutor.ask_typed(
                    ARENA_SYSTEM,
                    "This challenge's reference solution disagrees with some tests. Decide for each failure "
                    "whether the reference or the expectation is wrong according to the interface spec, and fix it. "
                    "Return the corrected reference_python and the COMPLETE tests list (same tests, fixed).\n\n"
                    f"Spec:\n{spec.model_dump_json(exclude={'reference_python', 'tests', 'hints', 'editorial', 'starter'})}"
                    f"\n\nReference:\n{spec.reference_python}\n\nTests:\n"
                    f"{json.dumps([t.model_dump() for t in spec.tests], ensure_ascii=False)}\n\nFailures:\n{detail}",
                    Repair, timeout=420)
                if unsafe_reason(fix.reference_python):
                    return None, 0
                spec.reference_python, spec.tests = fix.reference_python, fix.tests
                continue
            keep = [t for t, r in zip(spec.tests, results) if r["verdict"] == "OK"]
            if sum(not t.hidden for t in keep) >= 2 and sum(t.hidden for t in keep) >= 3:
                dropped = len(spec.tests) - len(keep)
                spec.tests = keep
                return spec, dropped
        return None, 0

    def _publish(self, spec: ChallengeSpec, language: str, dropped: int) -> dict:
        n = load_notes()
        base = "ch-" + kg.slugify(spec.title)[:50]
        slug, i = base, 2
        while slug in n or (WORK_DIR / slug).exists() or _spec_path(slug).exists():
            slug, i = f"{base}-{i}", i + 1
        today = dt.date.today().isoformat()
        (SECRET_DIR / slug).mkdir(parents=True, exist_ok=True)
        _spec_path(slug).write_text(json.dumps({"spec": spec.model_dump(), "meta": {
            "language": language, "created": today, "dropped_tests": dropped}}, indent=1, ensure_ascii=False),
            encoding="utf-8")
        work = WORK_DIR / slug
        (work / "tests").mkdir(parents=True, exist_ok=True)
        (work / LANGS[language]).write_text(spec.starter.strip() + "\n", encoding="utf-8")
        public = [t.model_dump(exclude={"hidden"}) for t in spec.tests if not t.hidden]
        (work / "tests" / "public.json").write_text(json.dumps(public, indent=1, ensure_ascii=False), encoding="utf-8")
        (work / "README.md").write_text(self._readme(slug, spec, language), encoding="utf-8")
        vault, _ = kg.load_config()
        meta = {"title": spec.title, "type": "challenge", "domain": TRACK_DOMAIN[spec.track], "track": spec.track,
                "kind": spec.kind, "language": language, "difficulty": spec.difficulty,
                "est_minutes": spec.est_minutes, "status": "open", "attempts": 0, "hints_used": 0,
                "created": today, "solved_on": None, "practices": spec.concepts, "tags": ["challenge"]}
        body = (f"# {spec.title}\n\n> {spec.story.strip().splitlines()[0] if spec.story.strip() else spec.title}\n\n"
                f"Workspace: `challenges/{slug}/` · difficulty {spec.difficulty} · {LANG_LABEL[language]}\n\n"
                "## What I learned\n\n_Design decisions, bugs you hit, what the review taught you._\n")
        write_note(vault / "challenges" / f"{slug}.md", meta, body)
        record({"event": "created", "slug": slug, "track": spec.track, "difficulty": spec.difficulty})
        return self.detail(slug)

    @staticmethod
    def _readme(slug: str, spec: ChallengeSpec, language: str) -> str:
        entry = LANGS[language]
        run = {"python": f"python {entry}", "c": "clang -std=c17 -O2 -o solution solution.c && ./solution",
               "cpp": "clang++ -std=c++20 -O2 -o solution solution.cpp && ./solution"}[language]
        if spec.kind == "http":
            run += " 8080"
        ex = []
        for i, t in enumerate((t for t in spec.tests if not t.hidden), 1):
            if spec.kind == "http":
                ex.append(f"**Example {i} — {t.name}**\n\n```\n{t.method} {t.path}\n{t.body}\n```\n"
                          f"Expected status {t.expect_status}, body ({t.match}):\n```\n{t.expect}\n```")
            else:
                parts = [f"**Example {i} — {t.name}**"]
                if t.args:
                    parts.append(f"Args: `{' '.join(t.args)}`")
                for f in t.files:
                    parts.append(f"File `{f.path}`:\n```\n{f.content}\n```")
                if t.stdin:
                    parts.append(f"Input:\n```\n{t.stdin}\n```")
                parts.append(f"Output ({t.match}):\n```\n{t.expect}\n```"
                             + (f"\nExit code: {t.expect_exit}" if t.expect_exit not in (0, -1) else ""))
                ex.append("\n\n".join(parts))
        return "\n".join([
            f"# {spec.title}", "", f"_{spec.track} · difficulty {spec.difficulty} · ~{spec.est_minutes} min · "
            f"{LANG_LABEL[language]}_", "", spec.story, "", "## Requirements", "", spec.statement, "",
            "## Interface", "", spec.interface, "", "## Constraints", "", *[f"- {c}" for c in spec.constraints], "",
            "## Examples", "", "\n\n".join(ex), "",
            "## How to work", "",
            f"Edit `{entry}` (add more files if you like). Run it yourself with `{run}`.",
            f"Judge from the repo root: `.\\kg.cmd judge {slug}` (public tests) or `.\\kg.cmd judge {slug} --submit` "
            "(all tests, counts for your rating) — or use the 🏁 Challenges page.", ""])

    # ---- judging

    async def _build(self, slug: str, language: str) -> tuple[list[str] | None, str]:
        work = WORK_DIR / slug
        if language == "python":
            entry = work / LANGS["python"]
            if not entry.exists():
                return None, f"{entry.name} not found"
            return [PYTHON, str(entry)], ""
        tc = toolchains()
        if language not in tc:
            return None, f"no working {LANG_LABEL[language]} compiler"
        ext = ".c" if language == "c" else ".cpp"
        sources = [p for p in work.rglob(f"*{ext}") if "tests" not in p.relative_to(work).parts]
        if not sources:
            return None, f"no {ext} files in {work.relative_to(kg.ROOT).as_posix()}"
        out = SECRET_DIR / slug / "build" / "solution.exe"
        out.parent.mkdir(parents=True, exist_ok=True)
        std = ["-std=c17"] if language == "c" else ["-std=c++20"]
        cmd = [tc[language], *std, "-O2", "-static", "-I", str(work), "-o", str(out), *map(str, sources)]
        if language == "c":
            cmd.append("-lm")
        r = await run_process(cmd, work, timeout=120)
        if r["timeout"] or r["exit"] != 0:
            return None, (r["stderr"] or r["stdout"] or "compiler timed out")[:4000]
        return [str(out)], ""

    async def judge(self, slug: str, submit: bool) -> dict:
        n = load_notes()
        note = n.get(slug)
        if not note or note.type != "challenge":
            raise TutorError("unknown challenge", 404)
        spec, meta = load_spec(slug)
        language = meta["language"]
        async with self._lock(slug):
            tests = spec.tests if submit else [t for t in spec.tests if not t.hidden]
            cmd, err = await self._build(slug, language)
            if cmd is None:
                verdict, results = "Compilation Error", []
            else:
                raw = await run_tests(cmd, spec.kind, tests)
                results = []
                for i, (t, r) in enumerate(zip(tests, raw), 1):
                    row = {"n": i, "name": t.name if not t.hidden else f"hidden test {i}", "hidden": t.hidden,
                           "verdict": r["verdict"], "ms": r.get("ms", 0)}
                    if not t.hidden:
                        row.update({"expect": t.expect, "match": t.match, "actual": r.get("stdout", ""),
                                    "stderr": r.get("stderr", ""), "exit": r.get("exit"), "status": r.get("status"),
                                    "expect_status": t.expect_status if spec.kind == "http" else None,
                                    "expect_exit": t.expect_exit if spec.kind == "cli" else None,
                                    "error": r.get("error", "")})
                    elif r["verdict"] != "OK" and (r.get("error") or r.get("stderr")):
                        row["error"] = (r.get("error") or r.get("stderr"))[:300]
                    results.append(row)
                first = next((x for x in results if x["verdict"] != "OK"), None)
                names = {"WA": "Wrong Answer", "TLE": "Time Limit Exceeded", "RE": "Runtime Error"}
                verdict = "Accepted" if not first else f"{names[first['verdict']]} on test {first['n']}"
            passed = sum(r["verdict"] == "OK" for r in results)
            status = note.meta.get("status", "open")
            event = {"slug": slug, "track": spec.track, "difficulty": spec.difficulty, "verdict": verdict,
                     "passed": passed, "total": len(tests)}
            rating_change = None
            if submit:
                attempts = int(note.meta.get("attempts") or 0) + 1
                fields = {"attempts": attempts}
                record({"event": "submit", **event})
                if verdict == "Accepted" and status == "open":
                    before = ratings()[spec.track]["rating"]
                    record({"event": "solved", **event, "wrong": attempts - 1,
                            "hints": int(note.meta.get("hints_used") or 0)})
                    rating_change = {"from": before, "to": ratings()[spec.track]["rating"]}
                    fields.update({"status": "solved", "solved_on": dt.date.today().isoformat()})
                update_fields(note, fields)
            else:
                record({"event": "run", **event})
            return {"verdict": verdict, "submit": submit, "passed": passed, "total": len(tests),
                    "compile_error": err if cmd is None else "", "results": results, "rating_change": rating_change,
                    "detail": self.detail(slug)}

    # ---- hints, give up, review

    def hint(self, slug: str) -> dict:
        note = load_notes().get(slug)
        if not note or note.type != "challenge":
            raise TutorError("unknown challenge", 404)
        spec, _ = load_spec(slug)
        used = int(note.meta.get("hints_used") or 0)
        if used >= len(spec.hints):
            raise TutorError("no more hints — try the editorial by giving up, or ask for a code review")
        if note.meta.get("status", "open") == "open":
            update_fields(note, {"hints_used": used + 1})
        return {"hint": spec.hints[used], "detail": self.detail(slug)}

    def give_up(self, slug: str) -> dict:
        note = load_notes().get(slug)
        if not note or note.type != "challenge":
            raise TutorError("unknown challenge", 404)
        spec, _ = load_spec(slug)
        if note.meta.get("status", "open") == "open":
            before = ratings()[spec.track]["rating"]
            record({"event": "gave-up", "slug": slug, "track": spec.track, "difficulty": spec.difficulty})
            update_fields(note, {"status": "gave-up"})
            return {"rating_change": {"from": before, "to": ratings()[spec.track]["rating"]},
                    "detail": self.detail(slug)}
        return {"rating_change": None, "detail": self.detail(slug)}

    async def review(self, slug: str) -> dict:
        note = load_notes().get(slug)
        if not note or note.type != "challenge":
            raise TutorError("unknown challenge", 404)
        spec, meta = load_spec(slug)
        work = WORK_DIR / slug
        exts = {"python": (".py",), "c": (".c", ".h"), "cpp": (".cpp", ".cc", ".hpp", ".h")}[meta["language"]]
        files, size = [], 0
        for p in sorted(work.rglob("*")):
            if p.suffix in exts and "tests" not in p.relative_to(work).parts and size < 40_000:
                text = p.read_text(encoding="utf-8", errors="replace")[:20_000]
                size += len(text)
                files.append(f"--- {p.relative_to(work).as_posix()}\n{text}")
        if not files:
            raise TutorError("no source files to review yet")
        last = next((e for e in reversed(history()) if e.get("slug") == slug and e.get("event") in ("submit", "run")),
                    None)
        prompt = (f"Review the learner's solution to this challenge like a senior engineer doing a kind but honest "
                  f"code review. Language: {LANG_LABEL[meta['language']]}.\n\nChallenge: {spec.title}\n"
                  f"{spec.statement}\n\nInterface:\n{spec.interface}\n\nLatest judge result: "
                  f"{(last or {}).get('verdict', 'not judged yet')} ({(last or {}).get('passed', 0)}/"
                  f"{(last or {}).get('total', 0)} tests)\n\nCode:\n" + "\n\n".join(files) +
                  "\n\nScore each rubric area 1-5 (include correctness, design, error handling, readability; add "
                  "security/performance/testing when relevant). Do not paste a full solution; quote at most a few "
                  "lines when pointing at an issue. If the solution is not accepted yet, point towards the bug "
                  "without giving the fix away.")
        r = await self.tutor.ask_typed(ARENA_SYSTEM, prompt, Review, timeout=300)
        for s in r.scores:
            s.score = max(1, min(5, s.score))
        return r.model_dump()


# --------------------------------------------------------------------------- CLI

def cli_judge(slug: str, submit: bool) -> int:
    class _NoTutor:
        async def ask_typed(self, *a, **k):
            raise TutorError("AI features need `kg serve`")
    try:
        r = asyncio.run(Arena(_NoTutor()).judge(slug, submit))
    except TutorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{'SUBMIT' if submit else 'RUN (public tests)'}: {r['verdict']}  ({r['passed']}/{r['total']} passed)")
    if r["compile_error"]:
        print(r["compile_error"])
    for x in r["results"]:
        mark = "✓" if x["verdict"] == "OK" else "✗"
        print(f"  {mark} {x['n']:>2}. {x['verdict']:<3} {x['ms']:>5} ms  {x['name']}")
        if x["verdict"] != "OK" and not x["hidden"]:
            print(f"       expected ({x['match']}): {x['expect'][:300]!r}\n       got: {x['actual'][:300]!r}")
            if x.get("stderr"):
                print(f"       stderr: {x['stderr'][:300]!r}")
    if r["rating_change"]:
        rc = r["rating_change"]
        print(f"rating: {rc['from']} → {rc['to']}")
    return 0 if r["verdict"] == "Accepted" else 1
