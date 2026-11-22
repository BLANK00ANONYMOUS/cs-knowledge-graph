#!/bin/sh
# Creates the local Python environment for the kg CLI and the app (macOS / Linux).
#   ./setup.sh                 core setup (claude-code, anthropic and openai providers)
#   ./setup.sh --with-copilot  also install the GitHub Copilot SDK and its runtime
#   ./setup.sh --no-hooks      don't turn on the repository's git hooks
set -eu
cd "$(dirname "$0")"
WITH_COPILOT=0; HOOKS=1
for a in "$@"; do
  case "$a" in
    --with-copilot) WITH_COPILOT=1 ;;
    --no-hooks) HOOKS=0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done
step() { echo "-> $1"; shift; "$@" || { echo "setup failed at: $*" >&2; exit 1; }; }
PY3="$(command -v python3 || command -v python)"
[ -d .venv ] || step "create .venv" "$PY3" -m venv .venv
PY=.venv/bin/python
step "check Python version (3.12+)" "$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
step "upgrade pip" "$PY" -m pip install --quiet --upgrade pip
step "install requirements.txt" "$PY" -m pip install --quiet -r requirements.txt
if [ "$WITH_COPILOT" = 1 ]; then
  step "install the Copilot SDK" "$PY" -m pip install --quiet -r requirements-copilot.txt
  step "download the Copilot runtime" "$PY" -m copilot download-runtime
fi
if [ "$HOOKS" = 1 ]; then
  # git will run .githooks/pre-commit on every commit in this clone. Read it first if you didn't write it.
  step "enable git hooks (.githooks/pre-commit: kg check + private-file guard)" git config core.hooksPath .githooks
fi
step "kg check" ./kg check
step "kg build" ./kg build
echo "Ready. Try: ./kg serve --open   (graph + AI tutor)   |   ./kg next"
