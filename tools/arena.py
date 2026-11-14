"""Development challenges arena: AI-set practical problems, a local judge, and per-track ratings.

A challenge is a small realistic engineering task (CLI tool, parser, stateful store, REST API...). The AI tutor writes
the statement, starter code, tests and a hidden Python reference solution; the tests are validated by running the
reference before the challenge is published. The learner solves it in any language from LANGUAGES whose toolchain
is installed (tests talk to the program only through stdin/stdout, args, files, exit codes or HTTP), the judge runs
the public + hidden tests (Codeforces-style verdicts), then the AI tutor reviews the code.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import inspect
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
BASE_RATING = 1200
OUT_LIMIT = 256 * 1024
TIME_LIMIT = 5.0      # seconds per test, plus the language's startup allowance

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


# --------------------------------------------------------------------------- languages

@dataclass(frozen=True)
class Lang:
    id: str
    label: str
    entry: str                      # file the starter code is written to
    probe: str                      # tiny program printing "ok": proves the toolchain really works
    sources: tuple[str, ...] = ()   # extensions compiled together; () = only the entry file
    review: tuple[str, ...] = ()    # extensions shown to the AI reviewer
    flat: bool = False              # only files in the folder root belong to the program (Go package)
    comment: str = "//"             # line-comment prefix for the starter header
    install: str = ""               # install hint shown in the UI while the toolchain is missing
    idiom: str = ""                 # what idiomatic starter code looks like (for the AI)
    run_hint: str = ""              # how the learner runs it by hand (challenge README)
    version: tuple[str, ...] = ()   # (toolchain key, *args) that prints the version
    startup: float = 0.0            # extra seconds per test for runtime startup (JVM, CLR, Node, interpreters)
    http: bool = False              # standard library can serve HTTP, so kind 'http' is allowed
    compile_timeout: float = 180.0
    run_env: dict = field(default_factory=dict)     # extra variables for the judged program (never secrets)
    build_env: dict = field(default_factory=dict)   # extra variables for the compiler


_CLANG_HINT = ("winget install MSYS2.MSYS2, then in the MSYS2 UCRT64 shell: pacman -S mingw-w64-ucrt-x86_64-clang "
               "and add C:\\msys64\\ucrt64\\bin to PATH")
_JDK_HINT = "winget install Microsoft.OpenJDK.21"
_NODE_HINT = "winget install OpenJS.NodeJS.LTS"
_JVM_STARTUP = 2.0

LANGUAGES: dict[str, Lang] = {x.id: x for x in [
    Lang("python", "Python", "solution.py", 'print("ok")', review=(".py",), comment="#",
         install="Python runs this app, so it is always available.",
         idiom="Python 3 standard library only; a main() function called under `if __name__ == \"__main__\":`; "
               "read input via sys.stdin / sys.argv, exit codes via sys.exit().",
         run_hint="python solution.py", version=("python", "--version"), http=True),
    Lang("c", "C", "solution.c", '#include <stdio.h>\nint main(void){puts("ok");return 0;}', sources=(".c",),
         review=(".c", ".h"), install=_CLANG_HINT,
         idiom="C17 with the C standard library only; `int main(int argc, char **argv)`; read input with "
               "fgets/scanf, write errors to stderr and return the exit code from main.",
         run_hint="clang -std=c17 -O2 -o solution solution.c && ./solution", version=("cc", "--version")),
    Lang("cpp", "C++", "solution.cpp", '#include <cstdio>\nint main(){std::puts("ok");}', sources=(".cpp", ".cc"),
         review=(".cpp", ".cc", ".hpp", ".h"), install=_CLANG_HINT,
         idiom="C++20 with the standard library only; `int main(int argc, char* argv[])`; "
               "std::ios::sync_with_stdio(false), std::getline/std::cin, std::string, std::vector, "
               "std::unordered_map; return the exit code from main.",
         run_hint="clang++ -std=c++20 -O2 -o solution solution.cpp && ./solution", version=("cc", "--version")),
    Lang("rust", "Rust", "solution.rs", 'fn main() { println!("ok"); }', review=(".rs",),
         install="winget install Rustlang.Rustup (plus the Visual Studio C++ Build Tools for the MSVC linker)",
         idiom="Rust 2021, a single crate rooted at solution.rs (extra modules via `mod name;` files next to it), "
               "std only — no crates; `fn main()`, read stdin with std::io::Read::read_to_string or BufRead::lines, "
               "args via std::env::args(), exit codes via std::process::exit; buffer output in a BufWriter.",
         run_hint="rustc -O --edition 2021 solution.rs && ./solution", version=("rustc", "--version")),
    Lang("go", "Go", "solution.go", 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("ok") }',
         sources=(".go",), review=(".go",), flat=True, install="winget install GoLang.Go",
         idiom="Go, one `package main` (files in the folder root) with `func main()`, standard library only "
               "(bufio, os, strings, strconv, fmt, sort, encoding/json, net/http); args via os.Args, exit codes via "
               "os.Exit; write through a bufio.Writer and Flush it before exiting.",
         run_hint="go run solution.go", version=("go", "version"), http=True,
         build_env={"CGO_ENABLED": "0", "GOTOOLCHAIN": "local", "GOFLAGS": "-buildvcs=false"}),
    Lang("java", "Java", "Main.java",
         'public class Main { public static void main(String[] a) { System.out.println("ok"); } }',
         sources=(".java",), review=(".java",), install=_JDK_HINT,
         idiom="Java 17+ with the JDK only, no build tool; `public class Main` with `public static void "
               "main(String[] args)` in Main.java and no package line; read stdin with a BufferedReader over "
               "System.in, collect output in a StringBuilder or PrintWriter, exit codes via System.exit. HTTP: "
               "com.sun.net.httpserver.HttpServer.",
         run_hint="javac Main.java && java Main", version=("javac", "-version"), startup=_JVM_STARTUP, http=True),
    Lang("kotlin", "Kotlin", "solution.kt", 'fun main() { println("ok") }', sources=(".kt",), review=(".kt",),
         install="scoop install kotlin (or unzip kotlin-compiler from github.com/JetBrains/kotlin/releases and add "
                 f"its bin folder to PATH), plus a JDK: {_JDK_HINT}",
         idiom="Kotlin/JVM with the Kotlin and Java standard libraries only; top-level `fun main(args: "
               "Array<String>)`; read stdin with System.`in`.bufferedReader() or readLine(), exit codes via "
               "kotlin.system.exitProcess. HTTP: com.sun.net.httpserver.HttpServer.",
         run_hint="kotlinc solution.kt -include-runtime -d solution.jar && java -jar solution.jar",
         version=("kotlinc", "-version"), startup=_JVM_STARTUP, http=True, compile_timeout=300),
    Lang("csharp", "C#", "solution.cs", 'System.Console.WriteLine("ok");', sources=(".cs",), review=(".cs",),
         install="winget install Microsoft.DotNet.SDK.10",
         idiom="C# 12+ on .NET with the base class library only (no NuGet packages); top-level statements or "
               "`static int Main(string[] args)`; read stdin with Console.In.ReadToEnd() or Console.ReadLine(), exit "
               "codes via Main's return value or Environment.Exit; build big outputs in a StringBuilder. The usual "
               "implicit usings (System, System.IO, System.Linq, System.Collections.Generic, "
               "System.Threading.Tasks) are available.",
         run_hint="dotnet run solution.cs", startup=1.5,
         run_env={"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"},
         build_env={"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"}),
    Lang("javascript", "JavaScript (Node.js)", "solution.js", 'console.log("ok");', review=(".js", ".mjs", ".cjs"),
         install=_NODE_HINT,
         idiom="Node.js with built-in modules only (no npm packages), CommonJS require(); read all of stdin with "
               "require('fs').readFileSync(0, 'utf8'), args via process.argv.slice(2), exit codes via "
               "process.exitCode. HTTP: the built-in http module.",
         run_hint="node solution.js", version=("node", "--version"), startup=1.0, http=True),
    Lang("typescript", "TypeScript (Node.js)", "solution.ts",
         'interface Msg { text: string }\nconst m: Msg = { text: "ok" };\nconsole.log(m.text);', review=(".ts",),
         install=f"{_NODE_HINT} (Node 22.6+ runs .ts files directly)",
         idiom="TypeScript run directly by Node's built-in type stripping — no tsc, tsconfig or npm packages; ES "
               "module syntax with node: built-ins, e.g. `import { readFileSync } from \"node:fs\"`; read stdin with "
               "readFileSync(0, \"utf8\"), args via process.argv.slice(2), exit codes via process.exitCode; local "
               "imports need the .ts extension. HTTP: node:http.",
         run_hint="node --experimental-transform-types solution.ts", version=("node", "--version"), startup=1.0,
         http=True),
    Lang("ruby", "Ruby", "solution.rb", 'puts "ok"', review=(".rb",), comment="#",
         install="winget install RubyInstallerTeam.Ruby.3.4",
         idiom="Ruby 3 standard library only (no gems); read input with $stdin.read or $stdin.each_line, args via "
               "ARGV, exit codes via exit(n), errors via warn.",
         run_hint="ruby solution.rb", version=("ruby", "--version"), startup=0.5),
    Lang("php", "PHP", "solution.php", '<?php echo "ok\\n";', review=(".php",), install="winget install PHP.PHP.8.4",
         idiom="a PHP 8 CLI script starting with `<?php`, core functions only (no Composer); read stdin with "
               "stream_get_contents(STDIN) or fgets(STDIN), args via $argv, exit codes via exit(n), errors via "
               "fwrite(STDERR, ...).",
         run_hint="php solution.php", version=("php", "--version"), startup=0.5),
    Lang("dart", "Dart", "solution.dart", "void main() { print('ok'); }", review=(".dart",),
         install="winget install Google.DartSDK",
         idiom="Dart 3 with dart: libraries only (no pub packages); `void main(List<String> args)`; read stdin with "
               "stdin.readLineSync(encoding: utf8) in a loop, set stdout.encoding = utf8 for non-ASCII output, exit "
               "codes via exit(n) from dart:io. HTTP: HttpServer.bind from dart:io.",
         run_hint="dart run solution.dart", version=("dart", "--version"), startup=0.5, http=True,
         compile_timeout=300),
]}
LANGS = {k: x.entry for k, x in LANGUAGES.items()}
LANG_LABEL = {k: x.label for k, x in LANGUAGES.items()}


def time_limit(language: str) -> float:
    return TIME_LIMIT + LANGUAGES[language].startup


def starter_file(language: str, slug: str, title: str, code: str) -> str:
    """The AI's starter code with a short header comment (kept after a shebang or `<?php` line)."""
    c = LANGUAGES[language].comment
    code = code.strip()
    fence = re.fullmatch(r"```[\w+#.-]*\n(.*)\n```", code, re.S)
    if fence:
        code = fence.group(1).strip()
    head = (f"{c} {title} — the full statement is in README.md.\n"
            f"{c} Judge: .\\kg.cmd judge {slug}   (add --submit to run the hidden tests too)\n")
    first, nl, rest = code.partition("\n")
    if first.startswith(("#!", "<?php")):
        return f"{first}\n{head}{rest}\n" if nl else f"{first}\n{head}"
    return f"{head}\n{code}\n"


# --------------------------------------------------------------------------- toolchains

PYTHON = getattr(sys, "_base_executable", None) or sys.executable  # venv python.exe on Windows is a launcher stub
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
_JUDGE_ENV = ["PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT", "LANG"]
# Compilers and SDK launchers also need their home/cache/install folders — still no tokens or other secrets.
_TOOL_ENV = ["USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "USERNAME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
             "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)",
             "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "JAVA_HOME", "KOTLIN_HOME", "RUSTUP_HOME",
             "CARGO_HOME", "RUSTUP_TOOLCHAIN", "GOROOT", "GOPATH", "GOCACHE", "GOMODCACHE", "DOTNET_ROOT", "PUB_CACHE"]
_EXE_KEYS = ("cc", "rustc", "go", "javac", "java", "kotlinc", "dotnet", "node", "ruby", "php", "dart")
RECHECK_S = 60   # missing toolchains are looked for again after this long (install without restarting the server)
TC_CACHE = kg.ROOT / ".kg" / "toolchains.json"
_TC: dict[str, dict] = {}
_TC_MISSED: dict[str, float] = {}
_TC_LOCKS = {k: threading.Lock() for k in LANGUAGES}
_TC_CACHE_LOCK = threading.Lock()


def judge_env(language: str | None = None, tc: dict | None = None) -> dict:
    """Minimal environment for judged programs: no tokens or secrets."""
    env = {k: os.environ[k] for k in _JUDGE_ENV if k in os.environ}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    if language:
        env.update(LANGUAGES[language].run_env)
        tc = tc if tc is not None else _TC.get(language)
        if tc and tc.get("bin"):   # toolchain installed after this process started (found via the registry PATH)
            env["PATH"] = os.pathsep.join([*tc["bin"], env.get("PATH", "")])
    return env


def tool_env(language: str, tc: dict | None = None) -> dict:
    """Environment for compilers: the judge environment plus the folders toolchains look for."""
    env = judge_env(language, tc)
    env.update({k: os.environ[k] for k in _TOOL_ENV if k in os.environ})
    env.update(LANGUAGES[language].build_env)
    return env


def _search_path() -> str:
    """This process's PATH plus the current user/machine PATH from the registry, so a toolchain installed while
    the server is running is found without restarting it."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if os.name == "nt":
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    parts += os.path.expandvars(str(winreg.QueryValueEx(k, "Path")[0])).split(os.pathsep)
            except OSError:
                continue
    return os.pathsep.join(dict.fromkeys(p.strip() for p in parts if p.strip()))


def _vkey(name: str) -> tuple:
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.-]", name)[:4])


def _dotnet_candidates(dotnet: str | None) -> list[dict]:
    """C# is compiled by calling the SDK's csc.dll directly against the newest reference pack with a matching
    runtime: about a second per build instead of several for `dotnet build`."""
    if not dotnet:
        return []
    root = Path(os.path.realpath(dotnet)).parent
    cscs = sorted((p for p in (root / "sdk").glob("*/Roslyn/bincore/csc.dll")),
                  key=lambda p: _vkey(p.parents[2].name), reverse=True)
    runtimes = sorted((p.name for p in (root / "shared" / "Microsoft.NETCore.App").glob("*")), key=_vkey, reverse=True)
    out = []
    for pack in sorted((root / "packs" / "Microsoft.NETCore.App.Ref").glob("*"), key=lambda p: _vkey(p.name),
                       reverse=True):
        mm = ".".join(pack.name.split(".")[:2])
        refs = pack / "ref" / f"net{mm}"
        rt = next((r for r in runtimes if r.startswith(mm + ".")), None)
        if cscs and rt and refs.is_dir():
            out.append({"dotnet": dotnet, "csc": str(cscs[0]), "refs": str(refs), "tfm": f"net{mm}",
                        "fx": f"{mm}.0", "version": f".NET {rt} · csc from SDK {cscs[0].parents[2].name}"})
    return out[:2]


def _candidates(lid: str, path: str) -> list[dict]:
    def w(name):
        return shutil.which(name, path=path)
    if lid == "python":
        return [{"python": PYTHON}]
    if lid in ("c", "cpp"):
        names = ("clang", "gcc", "cc") if lid == "c" else ("clang++", "g++", "c++")
        return [{"cc": exe} for exe in dict.fromkeys(w(n) for n in names) if exe]
    if lid == "csharp":
        return _dotnet_candidates(w("dotnet"))
    if lid == "typescript":
        node = w("node")
        flags = (["--experimental-transform-types", "--no-warnings"], ["--experimental-strip-types", "--no-warnings"],
                 ["--no-warnings"])
        return [{"node": node, "flags": f} for f in flags] if node else []
    tools = {"rust": ["rustc"], "go": ["go"], "java": ["javac", "java"], "kotlin": ["kotlinc", "java"],
             "javascript": ["node"], "ruby": ["ruby"], "php": ["php"], "dart": ["dart"]}[lid]
    found = {t: w(t) for t in tools}
    return [found] if all(found.values()) else []


def _sh(cmd: list[str], cwd: Path | None, env: dict, timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout, **_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None


def _bin_dirs(tc: dict) -> list[str]:
    """Folders of the toolchain's executables that aren't on this process's PATH (installed after it started)."""
    on_path = {p.strip().rstrip("\\/").lower() for p in os.environ.get("PATH", "").split(os.pathsep)}
    exes = [tc[k] for k in _EXE_KEYS if k in tc]
    return [d for d in dict.fromkeys(str(Path(e).parent) for e in exes) if d.lower() not in on_path]


def _detect(lid: str) -> dict | None:
    """First candidate toolchain that compiles and runs the language's probe program with the judge's settings."""
    lang = LANGUAGES[lid]
    for tc in _candidates(lid, _search_path()):
        exes = [tc[k] for k in _EXE_KEYS if k in tc]
        tc["bin"] = _bin_dirs(tc)
        with tempfile.TemporaryDirectory(prefix="kg-probe-", ignore_cleanup_errors=True) as tmp:
            work, out = Path(tmp), Path(tmp) / "build"
            out.mkdir()
            entry = work / lang.entry
            entry.write_text(lang.probe, encoding="utf-8")
            steps, run = _plan(lid, tc, [entry], entry, out)
            ok = True
            for cmd in steps:
                r = _sh(cmd, work, tool_env(lid, tc), min(lang.compile_timeout, 120))
                ok = bool(r) and r.returncode == 0
                if not ok:
                    break
            if ok:
                r = _sh(run, work, judge_env(lid, tc), 30)
                ok = bool(r) and r.returncode == 0 and r.stdout.decode(errors="replace").strip() == "ok"
        if not ok:
            continue
        tc["tool"] = exes[0] if exes else tc.get("python", "")
        if not tc.get("version") and lang.version:
            key, *args = lang.version
            r = _sh([tc[key], *args], None, tool_env(lid, tc), 30)
            text = (r.stdout + b"\n" + r.stderr).decode(errors="replace").strip() if r else ""
            tc["version"] = text.splitlines()[0].strip()[:100] if text else ""
        if tc.get("flags"):
            flags = " ".join(f for f in tc["flags"] if f != "--no-warnings")
            tc["version"] = f"{tc.get('version', '')} {flags}".strip()
        return tc
    return None


def _stamp(language: str, tc: dict) -> list:
    """What a cached toolchain result depends on: the judge code and the toolchain files' sizes and times."""
    code = "".join(inspect.getsource(f) for f in (_plan, _detect, _candidates, _dotnet_candidates))
    stamp: list = [hashlib.sha256((code + repr(LANGUAGES[language])).encode()).hexdigest()]
    for p in sorted(v for k, v in tc.items() if k in (*_EXE_KEYS, "python", "csc", "refs")):
        try:
            st = os.stat(p)
        except OSError:
            return []
        stamp.append([p, st.st_mtime_ns, st.st_size])
    return stamp


def _cached_toolchain(language: str) -> dict | None:
    try:
        entry = json.loads(TC_CACHE.read_text(encoding="utf-8")).get(language) or {}
    except (OSError, ValueError):
        return None
    tc = entry.get("tc")
    if not (tc and entry.get("stamp") and entry["stamp"] == _stamp(language, tc)):
        return None
    tc["bin"] = _bin_dirs(tc)
    return tc


def _save_toolchain(language: str, tc: dict) -> None:
    with _TC_CACHE_LOCK:
        try:
            data = json.loads(TC_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[language] = {"tc": tc, "stamp": _stamp(language, tc)}
        try:
            TC_CACHE.parent.mkdir(parents=True, exist_ok=True)
            TC_CACHE.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass


def toolchain(language: str) -> dict | None:
    """The working toolchain for a language. Results are cached in memory and in .kg/ (until the toolchain's files
    change); a missing toolchain is looked for again after RECHECK_S."""
    if language in _TC:
        return _TC[language]
    with _TC_LOCKS[language]:
        if language not in _TC and time.monotonic() - _TC_MISSED.get(language, float("-inf")) >= RECHECK_S:
            tc = _cached_toolchain(language)
            if not tc:
                try:
                    tc = _detect(language)
                except OSError:
                    tc = None
                if tc:
                    _save_toolchain(language, tc)
            if tc:
                _TC[language] = tc
            else:
                _TC_MISSED[language] = time.monotonic()
        return _TC.get(language)


def toolchains() -> dict[str, dict]:
    """Working toolchains by language id. Each language is checked (in parallel) by compiling and running a tiny
    program, because some installs are present but broken (e.g. a g++ whose cc1plus crashes)."""
    todo = [k for k in LANGUAGES if k not in _TC]
    if todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            list(ex.map(toolchain, todo))
    return dict(_TC)


_CS_PRELUDE = """// Added by the judge: the implicit usings of `dotnet run app.cs`; UTF-8 for redirected stdin/stdout.
global using System;
global using System.Collections.Generic;
global using System.IO;
global using System.Linq;
global using System.Net.Http;
global using System.Threading;
global using System.Threading.Tasks;

internal static class KgJudgeConsoleSetup
{
    [System.Runtime.CompilerServices.ModuleInitializer]
    internal static void Init()
    {
        var utf8 = new System.Text.UTF8Encoding(false);
        if (System.Console.IsInputRedirected)
            System.Console.SetIn(new System.IO.StreamReader(System.Console.OpenStandardInput(), utf8));
        if (System.Console.IsOutputRedirected)
            System.Console.SetOut(new System.IO.StreamWriter(System.Console.OpenStandardOutput(), utf8)
                                  { AutoFlush = true });
    }
}
"""
_JVM = ["-Xss64m", "-XX:+UseSerialGC", "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8",
        "-Duser.language=en", "-Duser.country=US"]


def _plan(lid: str, tc: dict, files: list[Path], entry: Path, out: Path) -> tuple[list[list[str]], list[str]]:
    """(compile steps, run command) for a solution; build output goes to `out` (which must exist)."""
    exe, src = str(out / "solution.exe"), [str(f) for f in files]
    if lid == "python":
        return [], [tc["python"], str(entry)]
    if lid == "c":
        return [[tc["cc"], "-std=c17", "-O2", "-static", "-I", str(entry.parent), "-o", exe, *src, "-lm"]], [exe]
    if lid == "cpp":
        return [[tc["cc"], "-std=c++20", "-O2", "-static", "-I", str(entry.parent), "-o", exe, *src]], [exe]
    if lid == "rust":
        return [[tc["rustc"], "-O", "--edition", "2021", "-C", "debuginfo=0", "-o", exe, str(entry)]], [exe]
    if lid == "go":
        return [[tc["go"], "build", "-o", exe, *src]], [exe]
    if lid == "java":
        classes = str(out / "classes")
        return ([[tc["javac"], "-encoding", "UTF-8", "-nowarn", "-d", classes, *src]],
                [tc["java"], *_JVM, "-cp", classes, "Main"])
    if lid == "kotlin":
        jar = str(out / "solution.jar")
        return [[tc["kotlinc"], *src, "-include-runtime", "-nowarn", "-d", jar]], [tc["java"], *_JVM, "-jar", jar]
    if lid == "csharp":
        dll, prelude, rsp = out / "solution.dll", out / "judge_prelude.cs", out / "csc.rsp"
        prelude.write_text(_CS_PRELUDE, encoding="utf-8")
        refs = sorted(Path(tc["refs"]).glob("*.dll"))
        rsp.write_text("\n".join([
            "-nologo", "-target:exe", "-optimize+", "-deterministic", "-langversion:latest", "-nullable:enable",
            "-warn:0", "-utf8output", f'-out:"{dll}"', *(f'-r:"{r}"' for r in refs), f'"{prelude}"',
            *(f'"{s}"' for s in src)]), encoding="utf-8")
        (out / "solution.runtimeconfig.json").write_text(json.dumps({"runtimeOptions": {
            "tfm": tc["tfm"], "framework": {"name": "Microsoft.NETCore.App", "version": tc["fx"]},
            "configProperties": {"System.Globalization.Invariant": True, "System.GC.Concurrent": False}}}),
            encoding="utf-8")
        return [[tc["dotnet"], tc["csc"], "-noconfig", f"@{rsp}"]], [tc["dotnet"], str(dll)]
    if lid == "javascript":
        return [], [tc["node"], str(entry)]
    if lid == "typescript":
        return [], [tc["node"], *tc["flags"], str(entry)]
    if lid == "ruby":
        return [], [tc["ruby"], "-EUTF-8", str(entry)]
    if lid == "php":
        return [], [tc["php"], "-d", "display_errors=stderr", str(entry)]
    if lid == "dart":
        return [[tc["dart"], "compile", "exe", str(entry), "-o", exe]], [exe]
    raise ValueError(f"unknown language {lid}")


_SKIP_DIRS = {"tests", "bin", "obj", "build", "out", "target", "node_modules", "__pycache__"}


def solution_files(work: Path, exts: tuple[str, ...], flat: bool = False) -> list[Path]:
    """The learner's source files (tests, build output and hidden folders are skipped)."""
    if not work.is_dir():
        return []
    found = []
    for p in sorted(work.iterdir() if flat else work.rglob("*")):
        if p.is_file() and p.suffix in exts and not any(
                d in _SKIP_DIRS or d.startswith(".") for d in p.relative_to(work).parts[:-1]):
            found.append(p)
    return found


def _rel(p: Path) -> str:
    try:
        return p.relative_to(kg.ROOT).as_posix()
    except ValueError:
        return str(p)


async def build_solution(language: str, work: Path, out: Path) -> tuple[list[str] | None, str]:
    """Compile the solution in `work` (output in `out`, reused while sources and toolchain are unchanged).
    Returns the command that runs it, or None and the compiler's message."""
    lang = LANGUAGES[language]
    tc = await asyncio.to_thread(toolchain, language)
    if not tc:
        return None, f"{lang.label} isn't installed on this machine (or its toolchain doesn't work). {lang.install}"
    entry = work / lang.entry
    if lang.sources:
        files = [f for f in solution_files(work, lang.sources, lang.flat) if not f.name.endswith("_test.go")]
        if not files:
            return None, f"no {'/'.join(lang.sources)} files in {_rel(work)}"
    elif entry.exists():
        files = [entry]
    else:
        return None, f"{lang.entry} not found in {_rel(work)}"
    out.mkdir(parents=True, exist_ok=True)
    steps, run = _plan(language, tc, files, entry, out)
    if not steps:
        return run, ""
    digest = hashlib.sha256(json.dumps([steps, tc.get("version", ""), _CS_PRELUDE if language == "csharp" else ""])
                            .encode())
    for f in files:
        digest.update(f.read_bytes())
    stamp = out / ".stamp"
    if stamp.exists() and stamp.read_text() == digest.hexdigest():
        return run, ""
    stamp.unlink(missing_ok=True)
    for cmd in steps:
        r = await run_process(cmd, work, timeout=lang.compile_timeout, env=tool_env(language, tc))
        if r["timeout"]:
            return None, f"compilation timed out after {lang.compile_timeout:.0f}s"
        if r["exit"] != 0:
            return None, ((r["stdout"] + "\n" + r["stderr"]).strip() or f"compiler exit code {r['exit']}")[:4000]
    if language == "java" and not (out / "classes" / "Main.class").exists():
        return None, ("no class Main: put `public class Main` with `public static void main(String[] args)` in "
                      "Main.java, without a package line")
    stamp.write_text(digest.hexdigest())
    return run, ""


async def run_solution(language: str, work: Path, out: Path, kind: str,
                       tests: list[TestCase]) -> tuple[str, list[dict] | None]:
    """Build and test a solution exactly as the judge does: (compile error, per-test results or None)."""
    cmd, err = await build_solution(language, work, out)
    if cmd is None:
        return err, None
    return "", await run_tests(cmd, kind, tests, timeout=time_limit(language), env=judge_env(language))


# --------------------------------------------------------------------------- process running


async def _kill_tree(p) -> None:
    """Kill a judged process and its children (Windows needs taskkill /T for that)."""
    if p.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec("taskkill", "/F", "/T", "/PID", str(p.pid),
                                                      stdout=asyncio.subprocess.DEVNULL,
                                                      stderr=asyncio.subprocess.DEVNULL, **_NO_WINDOW)
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


async def run_process(cmd: list[str], cwd: Path, stdin: str = "", timeout: float = TIME_LIMIT,
                      env: dict | None = None) -> dict:
    try:
        p = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), env=env if env is not None else judge_env(),
                                                 stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE, **_NO_WINDOW)
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


async def run_cli_test(cmd: list[str], t: TestCase, timeout: float, env: dict | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="kg-judge-") as tmp:
        work = Path(tmp)
        _setup_files(work, t.files)
        r = await run_process(cmd + t.args, work, t.stdin, timeout, env)
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


async def run_http_suite(cmd: list[str], tests: list[TestCase], timeout: float, env: dict | None = None) -> list[dict]:
    import aiohttp
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="kg-judge-") as tmp:
        work = Path(tmp)
        env = {**(env if env is not None else judge_env()), "PORT": str(port)}
        errfile = open(work / ".server-stderr.txt", "wb")
        try:
            p = await asyncio.create_subprocess_exec(*cmd, str(port), cwd=str(work), env=env,
                                                     stdin=asyncio.subprocess.DEVNULL,
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=errfile, **_NO_WINDOW)
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


async def run_tests(cmd: list[str], kind: str, tests: list[TestCase], timeout: float = TIME_LIMIT,
                    env: dict | None = None) -> list[dict]:
    if kind == "http":
        out = []
        for hidden in (False, True):   # each suite gets a fresh server; tests run in order
            suite = [t for t in tests if t.hidden == hidden]
            if suite:
                out += list(zip(suite, await run_http_suite(cmd, suite, timeout, env)))
        order = {id(t): i for i, t in enumerate(tests)}
        return [r for _, r in sorted(out, key=lambda x: order[id(x[0])])]
    sem = asyncio.Semaphore(4)

    async def one(t):
        async with sem:
            return await run_cli_test(cmd, t, timeout, env)
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
            "languages": [{"id": k, "label": x.label, "available": k in tc, "version": tc.get(k, {}).get("version", ""),
                           "tool": tc.get(k, {}).get("tool", ""), "install": x.install, "http": x.http,
                           "entry": x.entry} for k, x in LANGUAGES.items()],
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
        lang = LANGUAGES.get(language)
        if not lang:
            raise TutorError(f"language must be one of: {', '.join(LANGUAGES)}")
        if not await asyncio.to_thread(toolchain, language):
            raise TutorError(f"{lang.label} isn't installed on this machine — {lang.install}")
        n = load_notes()
        if concept and (concept not in n or n[concept].type != "concept"):
            raise TutorError(f"unknown concept '{concept}'")
        rating = ratings()[track]["rating"]
        difficulty = max(800, min(2400, int(difficulty or suggested_difficulty(rating))))
        existing = [x.title for x in n.values() if x.type == "challenge"]
        http_rule = (f"Use kind 'http' only when the task is naturally an HTTP service ({lang.label} can serve HTTP "
                     "with its standard library)." if lang.http else
                     f"Use kind 'cli' (HTTP-service challenges aren't supported for {lang.label}).")
        prompt = "\n".join([
            f"Write ONE new challenge. Track: {track}. Learner's language: {lang.label}. "
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
            "self-contained). " + http_rule,
            *(["- Backend track: backend skills are language-agnostic and the judge talks to the program only "
               "through stdin/stdout, args, files and exit codes, so any language works. Model backend work as a "
               "program that handles requests or commands — e.g. a request router or rate limiter fed one request per "
               "line, a session or key-value store driven by commands, an API payload validator/transformer (JSON, "
               "CSV), a job queue or migration planner — with precise error responses."] if track == "backend" else []),
            "- Solvable with the language's standard library only. Deterministic output (no timestamps/randomness; "
            "if line order is unspecified use match 'lines'). Keep outputs short.",
            "- tests: 3-5 public examples (hidden=false) and 6-10 hidden tests (hidden=true) covering edge cases, "
            "invalid input and error handling. Expected outputs must follow the interface EXACTLY.",
            "- reference_python: complete, correct Python 3 standard-library solution. No network except serving "
            "the HTTP port, no subprocess/eval/exec, never delete files or use absolute paths.",
            f"- starter: idiomatic {lang.label} saved as `{lang.entry}` — {lang.idiom} It must compile/run as-is, "
            "show the program skeleton (input parsing, output, exit codes) with TODO comments, and must NOT solve "
            "the task. Return only the code (no markdown fences).",
            "- Difficulty guide: 800 ≈ 30 lines, few requirements; 1200 ≈ 80 lines with careful edge cases; "
            "1600 ≈ several components or state; 2000+ ≈ substantial design.",
        ])
        last_error = ""
        for attempt in range(2):
            spec = await self.tutor.ask_typed(ARENA_SYSTEM, prompt + last_error, ChallengeSpec, timeout=480)
            spec.difficulty = difficulty
            spec.track = track
            if not lang.http:
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
        (work / LANGS[language]).write_text(starter_file(language, slug, spec.title, spec.starter), encoding="utf-8")
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
        run = LANGUAGES[language].run_hint
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

    async def judge(self, slug: str, submit: bool) -> dict:
        n = load_notes()
        note = n.get(slug)
        if not note or note.type != "challenge":
            raise TutorError("unknown challenge", 404)
        spec, meta = load_spec(slug)
        language = meta["language"]
        if language not in LANGUAGES:
            raise TutorError(f"unsupported language '{language}'")
        async with self._lock(slug):
            tests = spec.tests if submit else [t for t in spec.tests if not t.hidden]
            err, raw = await run_solution(language, WORK_DIR / slug, SECRET_DIR / slug / "build" / language,
                                          spec.kind, tests)
            if raw is None:
                verdict, results = "Compilation Error", []
            else:
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
                    "compile_error": err, "results": results, "rating_change": rating_change,
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
        lang = LANGUAGES.get(meta["language"], LANGUAGES["python"])
        files, size = [], 0
        for p in solution_files(work, lang.review):
            if size >= 40_000:
                break
            text = p.read_text(encoding="utf-8", errors="replace")[:20_000]
            size += len(text)
            files.append(f"--- {p.relative_to(work).as_posix()}\n{text}")
        if not files:
            raise TutorError("no source files to review yet")
        last = next((e for e in reversed(history()) if e.get("slug") == slug and e.get("event") in ("submit", "run")),
                    None)
        prompt = (f"Review the learner's solution to this challenge like a senior engineer doing a kind but honest "
                  f"code review. Language: {lang.label}.\n\nChallenge: {spec.title}\n"
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
