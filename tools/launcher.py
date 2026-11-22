"""Launch the knowledge-graph app like a normal desktop application.

    launcher.py               start the server in the background if needed, then open the app window
    launcher.py --stop        stop the background server this checkout started
    launcher.py --url /jobs   open a specific page
    launcher.py --server-only start the server without a window (used by the optional auto-start shortcut)

Run by the Start Menu / Desktop shortcut that tools/install.ps1 creates. Uses only the standard library, so it can
explain what is wrong even when the virtual environment is missing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KG_DIR = ROOT / ".kg"
PID_FILE = KG_DIR / "server.pid"
TOKEN_FILE = KG_DIR / "server.token"
LOCK_FILE = KG_DIR / "launcher.lock"
LOG_FILE = KG_DIR / "server.log"
LOG_MAX = 2 * 1024 * 1024
ROOT_ID = hashlib.sha1(str(ROOT).encode()).hexdigest()[:12]   # same id the server reports in /api/health
WINDOWS = os.name == "nt"
# Give the server its own hidden console (children inherit it, so `claude -p` or `gh` don't flash windows).
# DETACHED_PROCESS would leave it with no console at all, and Windows ignores CREATE_NO_WINDOW combined with it.
SPAWN_FLAGS = (0x08000000 | 0x00000200) if WINDOWS else 0    # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
NO_WINDOW = 0x08000000 if WINDOWS else 0


def fail(msg: str) -> None:
    """Show the problem even when started from a shortcut (pythonw has no console)."""
    if WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "Knowledge Graph", 0x10)
        except Exception:
            pass
    raise SystemExit(msg)


def venv_python(windowless: bool = False) -> Path | None:
    for rel in ((".venv/Scripts/pythonw.exe" if windowless else ".venv/Scripts/python.exe"),
                ".venv/Scripts/python.exe", ".venv/bin/python"):
        p = ROOT / rel
        if p.exists():
            return p
    return None


def port() -> int:
    """tutor.port from kg.config.yaml, read without importing yaml (the launcher must work without the venv)."""
    try:
        text = (ROOT / "kg.config.yaml").read_text(encoding="utf-8-sig")
        m = re.search(r"(?ms)^tutor:\s*\n(.*?)(?=^\S|\Z)", text)
        if m:
            p = re.search(r"(?m)^\s+port:\s*(\d+)", m.group(1))
            if p:
                return int(p.group(1))
    except OSError:
        pass
    return 8765


def health(p: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/health", timeout=2) as r:
            data = json.load(r)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def ours(p: int) -> bool:
    """True only for this checkout's server: any other program (or another clone) on the port doesn't count."""
    data = health(p)
    return bool(data) and data.get("ok") is True and data.get("root") == ROOT_ID


def token() -> str:
    try:
        return str(json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("token") or "")
    except (OSError, ValueError):
        return ""


def rotate_log() -> None:
    try:
        if LOG_FILE.stat().st_size > LOG_MAX:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
    except OSError:
        pass


class StartLock:
    """Two quick launches must not both start a server (one would fail on the port)."""

    def __enter__(self):
        deadline = time.time() + 40
        while True:
            try:
                self.fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - LOCK_FILE.stat().st_mtime > 60:   # left behind by a crashed launcher
                        LOCK_FILE.unlink()
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    fail("Another launch is still starting the server; try again in a moment.")
                time.sleep(0.3)

    def __exit__(self, *exc):
        os.close(self.fd)
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


def start_server(p: int, idle_minutes: float) -> None:
    py = venv_python()
    if not py:
        fail("The app isn't set up yet: run setup.ps1 (Windows) or setup.sh (macOS/Linux) first.")
    rotate_log()
    log = open(LOG_FILE, "ab")
    args = [str(py), str(ROOT / "tools" / "kg.py"), "serve", "--port", str(p)]
    if idle_minutes > 0:
        args += ["--idle-exit", str(idle_minutes)]
    popen = {"creationflags": SPAWN_FLAGS} if WINDOWS else {"start_new_session": True}
    proc = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **popen)
    PID_FILE.write_text(json.dumps({"pid": proc.pid, "port": p, "root": ROOT_ID}), encoding="utf-8")
    for _ in range(120):
        if proc.poll() is not None:
            fail(f"The server stopped right away (exit code {proc.returncode}). See {LOG_FILE}")
        if ours(p) and token():
            return
        time.sleep(0.25)
    fail(f"The server didn't start within 30 seconds. See {LOG_FILE}")


def _cmdline(pid: int) -> str:
    try:
        if WINDOWS:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                                 capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
            return out.stdout
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def stop_server() -> bool:
    """Stop the server this checkout started, after checking that the recorded process really is it."""
    try:
        pid = int(json.loads(PID_FILE.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        try:
            pid = int(json.loads(TOKEN_FILE.read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
    cmd = _cmdline(pid)
    if "kg.py" not in cmd or "serve" not in cmd:
        return False   # not ours (the pid was reused, or the server already exited): never kill it
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, creationflags=NO_WINDOW)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    for f in (PID_FILE,):
        try:
            f.unlink()
        except OSError:
            pass
    return True


def browser() -> str | None:
    for name in ("chrome", "msedge", "google-chrome", "chromium", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    if WINDOWS:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
                if base and (Path(base) / rel).exists():
                    return str(Path(base) / rel)
    elif sys.platform == "darwin":
        for app in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
            if Path(app).exists():
                return app
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--server-only", action="store_true", help="start the server but don't open a window")
    ap.add_argument("--url", default="/", help="page to open, e.g. /jobs (a path starting with one '/')")
    ap.add_argument("--idle-exit", type=float, default=240,
                    help="minutes without use before the background server stops itself (0 = never)")
    args = ap.parse_args()
    KG_DIR.mkdir(exist_ok=True)
    if args.stop:
        return 0 if stop_server() else 1
    if not re.fullmatch(r"/[A-Za-z0-9_\-/]*(\?[\w=&%.,\-]*)?(#[\w\-]*)?", args.url) or args.url.startswith("//"):
        fail(f"--url must be a path inside the app, like /jobs (got {args.url!r})")
    p = port()
    with StartLock():
        if not ours(p):
            if health(p):
                fail(f"Port {p} is used by another program (or another copy of this app). Stop it, or set a "
                     "different tutor.port in kg.config.yaml.")
            start_server(p, 0 if args.server_only else args.idle_exit)
    if args.server_only:
        return 0
    key = token()
    if not key:
        fail(f"The server is running but its key file is missing; stop it (--stop) and start again. See {LOG_FILE}")
    sep = "&" if "?" in args.url else "?"
    url = f"http://127.0.0.1:{p}{args.url}{sep}kgtoken={key}"
    exe = browser()
    if exe:   # its own window and profile: your everyday extensions can't read or change the app's pages
        profile = KG_DIR / "browser-profile"
        subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                          "--no-default-browser-check"], creationflags=NO_WINDOW)
    else:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
