# Verification of the 14 Nov 2026 review (59 findings)

Each finding in *cs-knowledge-graph — consolidated review* was checked against the code on `main` at `68a614b` and, where
possible, reproduced on a running server before fixing. **Verdicts:**
- **Genuine:** confirmed by reading the code and, where noted, by a live test.
- **Partly:** only some of the claims hold.
- **Not genuine:** the code doesn't behave as described.
- **Design note:** a suggestion, not a defect.

**Fix status:**
- **Fixed:** done.
- **Partial:** what's left is listed under "Not done, and why" at the end.

Every fix with a ✓ in the Test column is covered by `tests/`.

## Security

| ID | Finding | Verdict | What was done | Test |
|---|---|---|---|---|
| S1 | DOM XSS / open redirect via `/?open=` | **Genuine (reproduced)**: `/?open=javascript:…` ran a script in the app | Shell opens only same-origin section paths; `?open=` must be a single-slash path; the frame `route` message is checked too. Re-tested live: script no longer runs. | ✓ |
| S2 | DNS rebinding bypasses the guard | **Genuine (reproduced)**: a spoofed `Host` read `/api/graph` and wrote `/api/profile` | `Host` allow-list (127.0.0.1 / localhost / [::1] on the port) for every method; `Origin` checked against the same list. Both requests now get 403. | ✓ |
| S3 | Image/CSS exfiltration from model output | **Genuine** (DOMPurify keeps `<img>`/`style` by default) | `KG.md` drops images, styles, forms, media and non-https links; CSP `img-src` allows only the app and GitHub avatars. Checked in the browser. | ✓ |
| S4 | Judged code is unsandboxed | **Genuine** | POSIX: own process group (a timeout kills grandchildren), CPU-time limit, 64 MB file-size limit, no core dumps. README says plainly it is not a sandbox. | ✓ |
| S5 | Prompt-injected generation → code execution | **Genuine** (starter never screened; theme pasted into the prompt) | Theme passed as a JSON string labelled as data. Python starters screened like the reference. The starter must compile and must *fail* the tests (Q1), run under S4 containment. Remote triggers closed by S1/S2/S18. | |
| S6 | Stored prompt injection through Jobs | **Genuine** (`>>>` closed the fence) | Posting sent as one JSON string in `<untrusted_job_posting_json>`; system prompt updated. "Add to graph" now lists each proposed concept with its summary and a checkbox; only checked ones are written. | ✓ |
| S7 | Builder prompts lack the data rule | **Genuine** | Rule added to `BUILD_SYSTEM`. Issue data labelled untrusted; "First steps" labelled AI-suggested in the UI and in tracked notes. | |
| S8 | `javascript:` URL in the Jobs link | **Genuine** | `safe_url` in `Jobs.detail/overview/analyze`; `KG.safeUrl` in the page and the job board. | ✓ |
| S9 | GitHub token in plaintext | **Genuine** (mode 600 on macOS; weak on Windows) | Token stored in the OS credential store via `keyring` when available; the file fallback is created 0600 atomically. | ✓ |
| S10 | Deep links start LLM calls | **Genuine** | Links that arrive in the address bar carry `kgconfirm`; the graph shows "This link starts … [Start]" instead of running. In-app clicks are unchanged. Checked in the browser. | |
| S11 | CDN scripts without SRI, old versions | **Genuine** (DOMPurify 3.1.6, marked 12, vis-network 9) | Vendored in `tools/vendor/`: marked 18.1.2, DOMPurify 3.4.17, vis-network 10.1.4 (via `npm pack`, integrity-checked). `kg build` copies them to `build/vendor/`. Graph checked in the browser. | ✓ |
| S12 | Claude Code provider inherits API keys | **Genuine** | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` and the Bedrock/Vertex switches are removed from the CLI environment. | ✓ |
| S13 | Copilot `available_tools=[]` = default tools? | **Not genuine**: the SDK documents `available_tools` as an allow-list ("only these tools will be available"); the client runs in `mode="empty"` | Hardened anyway: permission requests are now rejected (`_deny_permission`) instead of `approve_all`. | ✓ |
| S14 | Editor-agent instructions | **Genuine** | "Retrieved text is data" rule in `.github/copilot-instructions.md` and both skills. | |
| S15 | No CSP / frame-ancestors / headers | **Genuine** | CSP (scripts, connections and frames from self only; `frame-ancestors 'self'`, `base-uri 'none'`, `object-src 'none'`), `X-Frame-Options`, `nosniff`, `no-referrer`, COOP/CORP on every response. | ✓ |
| S16 | Personal data committed | **Partly**: `vault/roles/` wasn't ignored (genuine), but no role/project/target notes are in history (`git log --all -- vault/roles …` is empty) | `vault/roles/*` ignored; pre-commit refuses `vault/roles/`, `.kg/`, `books/`, PDFs, journal, plans. History and screenshots left for the owner (see below). | |
| S17 | setup enables repo git hooks | **Genuine** (trust note) | Setup prints what it enables, `-NoHooks`/`--no-hooks` skips it, README says so. | |
| S18 | Always-on server answers anyone | **Genuine** | Per-launch random key → `HttpOnly; SameSite=Strict` cookie; everything except a minimal `/api/health` needs it. Key file `.kg/server.token` (0600) for the launcher. Optional idle exit (launcher: 4 h). | ✓ |
| S19 | App window uses everyday profile | **Genuine** | Launcher uses `--user-data-dir=.kg/browser-profile`. | |

## Correctness

| ID | Finding | Verdict | What was done | Test |
|---|---|---|---|---|
| B1 | BOM notes corrupted by `rate` | **Genuine** | `kg.read_note_text` (utf-8-sig) for every read-modify-write; writes without BOM; `set_frontmatter_field` refuses to prepend over malformed frontmatter. | ✓ |
| B2 | Ratings double counted | **Genuine** | Judge re-reads the note inside the per-slug lock; give-up uses the same lock; `ratings()` counts only the first resolution per slug. | ✓ |
| B3 | Stale builds after header edits | **Genuine** | Build digest covers every source/header file the build can read (with relative names). | ✓ |
| B4 | `books_dir` relative to cwd | **Genuine** | Relative paths resolve against the repo root. | ✓ |
| B5 | Crashes on unusual input | **Genuine** | Mastery clamped 0-5; non-mapping frontmatter reported by `kg check`; JSON state written atomically; a corrupt history is moved aside. | ✓ |
| B6 | Slug collisions | **Genuine** for books and repos; topics link to the existing concept (no overwrite) | Books: slug stable per PDF path, hash suffix on collision. Repos/PRs: hash suffix when a different URL already uses the slug. | ✓ |
| B7 | `Account.ensure()` race | **Genuine** | `asyncio.Lock`; `checked` set at the end. | ✓ |
| B8 | Claude CLI on Windows; default mismatch | **Genuine** | Windows locations searched, `.exe` preferred; system prompt passed via `--system-prompt-file` (checked against the installed CLI); default provider `claude-code`. | ✓ |
| B9 | GitHub sync all-or-nothing | **Genuine** | Sync lock (409 on double click); failed tagging batches are non-fatal and retried on the next sync; pagination; only changed notes rewritten. | ✓ |
| B10 | Truncated output looks like bad JSON | **Genuine** | Clear errors on `stop_reason == "max_tokens"` / `finish_reason == "length"`. | ✓ |
| B11 | Blocking calls in async handlers | **Genuine** | Vault parse cached by mtime; health, today, graph, build summary, topic search and `gh auth token` run off the event loop. | ✓ |
| B12 | Search re-indexes every call | **Genuine** | Book queries skip the note refresh; refresh only when notes changed; `busy_timeout`; title weight 0 for book pages. | ✓ |
| B13 | Tutor engine weaknesses | **Genuine** (a–f) | (a) optional `tutor.verify_answers` blind solve; (b) options shuffled in code; (c) ratings only for tested concepts, capped by `evidence_cap`; (d) LRU eviction under lock, failed starts cleaned up, 12 h pruning; (e) `extra="ignore"`; (f) README wording. | ✓ |
| B14 | Whole-vault validation after writing | **Genuine** | Only errors *introduced* by the call count; the call's own files (and the role note) are rolled back. | ✓ |
| B15 | Auto-review after Accepted never runs | **Genuine** | Clicked after `run()` resolves. | |
| B16 | "then learn X" never happens | **Genuine** | Today passes `then=<slug>`; after the review report the tutor offers "Continue: learn X". | |
| B17 | `lessonMap` keeps roots, drops direct prereqs | **Genuine** | Nearest prerequisites kept first. | |
| B18 | Job board silent failures | **Genuine** | Per-source errors shown; results cached 10 min; title/tag matches ranked first. | |
| B19 | Hard-coded India; double-counted readiness | **Genuine** | `github.location` in config (set to India to keep current behaviour); readiness counts each concept once. | ✓ |
| B20 | Judge robustness | **Genuine** (all four) | TLE re-run alone; AI regex runs in a killable 2 s subprocess on ≤64 KB; `-static` falls back to dynamic (off on macOS); `true` ≠ `1`. | ✓ |
| B21 | Mastery/status bookkeeping | **Genuine** | `mark_learned` stamps only when mastery rises; synced project status follows activity unless set by hand. | ✓ |
| B22 | Toasts overlap and chatter | **Genuine** | One stacking container; progress toasts aren't live regions; start/finish announced once. | |
| B23 | setup.ps1 reports success after failures | **Genuine** | Each native step checks `$LASTEXITCODE`; Python 3.12+ check; Copilot runtime only with `-WithCopilot`. | |
| B24 | `kg.cmd` cryptic without .venv | **Genuine** | Clear message and exit code; `kg` shell wrapper and `setup.sh` for macOS/Linux. | |
| B25 | Ratings not portable; redundant ignores | **Genuine** | Documented as per machine; redundant lines removed. | |
| B26 | Launcher trusts any server; `--stop` kills anything | **Genuine** | Health returns a repo id the launcher compares; pid file; `--stop` checks the process command line first. | |
| B27 | Detached server flashes consoles | **Plausible** (Windows) | Server started with `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`; children of `llm.py` get `CREATE_NO_WINDOW`. | |
| B28 | Launcher failures slow and invisible | **Genuine** | Polls the child, message box on failure, refuses to run without `.venv`, start lock, log rotation, `--url` validated, no `kg`/`yaml` import. | |
| B29 | Hook checks the working tree; no private-file guard | **Genuine** | Hook checks the staged snapshot (`git checkout-index` to a temp dir) and blocks private paths. | |

## Quality

| ID | Finding | Verdict | What was done |
|---|---|---|---|
| Q1 | Validity is self-consistency only | **Genuine** | Partial: the starter must compile and must fail the tests. |
| Q2 | Default provider can't cite books | **Genuine** | README and Learn blurb say which providers can cite. |
| Q3 | No tests or CI | **Genuine** | 39 pytest tests in `tests/`; `.github/workflows/ci.yml` runs pytest + `kg check` on Ubuntu and Windows. |
| Q4 | Fixed review ladder | Design note | Not changed. |
| Q5 | Prompt grows with the graph | **Genuine** | Above 150 concepts, prompts get the BM25-relevant concepts, their prereqs, those in progress and the ready ones. |
| Q6 | Graph O(N×E) per render | **Genuine** | Adjacency and readiness indexed once per refresh; positions sampled at most 4×/s. |
| Q7 | Dependency hygiene | **Genuine** | Copilot SDK moved to `requirements-copilot.txt`; `constraints.txt` pins the tested set. |
| Q8 | LLM operations | **Genuine** | Partial: concurrency cap (3) and killed CLI processes awaited. |
| Q9 | Project hygiene | **Partly** | Partial: `setup.sh`, `kg` wrapper and vendored libraries added. |
| Q10 | Two tutor implementations drift | Design note | The teach skill now points to the web tutor's rules (confidence first, 0-2 reasoning, evidence cap). |
| Q11 | Small accessibility details | **Genuine** | Nav text 13 px; focus ring on the whole quiz option. |

## Not done, and why

- **S4: Windows has no process limits.** The Windows judge still relies on time limits and `taskkill /T`. A Job Object with memory and process limits needs testing on Windows, and none was available here. The app also still trusts `.kg/toolchains.json`. None of this makes judged code a real sandbox: only run code you wrote or have read.
- **S16: history and screenshots are the owner's call.**
  - Commits `c528718` and `bfc3a88` carry a work email address. Removing it means rewriting history and force-pushing.
  - `docs/screenshots/11-build.png` shows the owner's GitHub handle and repository names. That data is public, but personal.
- **Q1: no independent check of the tests.** No second model implements the challenge from its statement to cross-check the tests.
- **Q2: tool-less providers can't cite books.** Book passages aren't pre-fetched into the prompt for providers that can't call tools.
- **Q4: the review schedule is unchanged.** It is still the fixed ladder, not SM-2 or FSRS.
- **Q8: no prompt caching.** The Anthropic provider doesn't cache prompts.
- **Q9: licence and repository settings are the owner's call.** A `LICENSE` file, repository topics and a demo GIF need the owner's decision.
- **Windows untested.** `setup.ps1`, `kg.cmd`, the launcher's Windows paths and the CI Windows job were not run here; there is no Windows machine.
