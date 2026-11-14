"""Launch the knowledge-graph app like a normal desktop application.

    launcher.py            start the server in the background if needed, then open the app window
    launcher.py --stop     stop the background server
    launcher.py --url /jobs  open a specific page

Run by the Start Menu / Desktop shortcut that tools/install.ps1 creates.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kg  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)


def port() -> int:
    return int((kg.load_settings().get("tutor") or {}).get("port", 8765))


def healthy(p: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/health", timeout=2) as r:
            return bool(json.load(r).get("ok", True))
    except Exception:
        return False


def start_server(p: int) -> None:
    log = open(ROOT / ".kg" / "server.log", "ab")
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    subprocess.Popen([str(py if py.exists() else sys.executable), str(ROOT / "tools" / "kg.py"), "serve", "--port", str(p)],
                     cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                     env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    for _ in range(60):
        if healthy(p):
            return
        time.sleep(0.5)
    raise SystemExit("The server did not start; see .kg\\server.log")


def stop_server(p: int) -> None:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"(Get-NetTCPConnection -LocalPort {p} -State Listen -ErrorAction SilentlyContinue).OwningProcess"],
                         capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    for pid in {x for x in out.split() if x.isdigit()}:
        subprocess.run(["taskkill", "/PID", pid, "/F", "/T"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def browser() -> str | None:
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
            if base and (Path(base) / rel).exists():
                return str(Path(base) / rel)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--server-only", action="store_true", help="start the server but don't open a window")
    ap.add_argument("--url", default="/")
    args = ap.parse_args()
    (ROOT / ".kg").mkdir(exist_ok=True)
    p = port()
    if args.stop:
        stop_server(p)
        return 0
    if not healthy(p):
        start_server(p)
    if args.server_only:
        return 0
    url = f"http://127.0.0.1:{p}{args.url}"
    exe = browser()
    if exe:   # own window without tabs or address bar, remembers its size like an installed app
        subprocess.Popen([exe, f"--app={url}"], creationflags=NO_WINDOW)
    else:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
