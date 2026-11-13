# 🧠 knowledge-graph

My personal teaching environment for **computer science & mathematics** — a Markdown knowledge graph of
concepts linked by prerequisites, grounded in my own PDF library, with GitHub Copilot as the tutor.

Current focus: **AI · Security · Systems programming** (+ the math underneath).

## Quick start

```powershell
.\setup.ps1                 # creates .venv, installs deps, validates and builds the graph
.\kg.cmd serve --open       # 🏠 Today · 🧠 Graph + 🎓 tutor · 🛠 Build · 🏁 Challenges at http://127.0.0.1:8765
.\kg.cmd next               # what am I ready to learn?
```

Then, in a Copilot session in this repo: **"Teach me backpropagation"**, **"Quiz me on what's due"**,
or **"Ingest my new books"**.

## 🎓 AI tutor (`kg serve`)

`kg serve` runs three pages on `http://127.0.0.1:8765` — **🏠 Today** (`/`), **🧠 Graph** (`/graph`, with the tutor
panel) and **🛠 Build** (`/build`).

### 📘 Learn mode (learn by doing — no book reading required)

Pick a concept → **📘 Learn**. Copilot writes a bite-size lesson **in its own words**, fact-checked against your PDF
library (it searches your books first and shows the cited pages as *verified* badges — the books are its reference,
not your homework):

1. 3–5 short parts, each with a worked example (C/C++ for systems/security, Python for AI/maths) and an instant
   quick-check question (retrieval right after each chunk).
2. Key takeaways, then **🛠 Apply it** — a 20–60 minute hands-on exercise with success criteria. Paste your
   code/output and press **🔍 Review my work** for feedback.
3. **✅ Mark as learned** (mastery ≥ 1) or **🎓 Quiz me** to earn your real mastery level.

Today's session starts with **Learn** for new concepts and with mixed **Quiz** reviews for concepts that are due.

## 🏁 Challenges (`/arena`) — Codeforces for development

Practical development problems instead of algorithm puzzles: CLI tools, parsers for real formats, stateful stores,
REST APIs (Python), security and systems tasks.

1. **Generate** — pick a track (backend / systems / security / ai / tooling), language (Python, C, C++), difficulty
   (800–2400, suggested from your rating) and optionally a concept to practise. Copilot writes the statement,
   interface, starter code, public examples, hidden tests and a **hidden Python reference solution**. The challenge
   is only published after the reference passes every test (disagreements get one AI repair round; tests that still
   disagree are dropped), so the judge can be trusted.
2. **Solve** in your editor: `challenges/<slug>/solution.{py,c,cpp}` (README.md has the full statement).
3. **Judge** — **▶ Run** public tests or **🚀 Submit** for public + hidden tests, from the page or the terminal:
   `.\kg.cmd judge <slug>` / `.\kg.cmd judge <slug> --submit`. Verdicts: Accepted / Wrong Answer / Time Limit
   Exceeded / Runtime Error / Compilation Error, with a diff for failing public tests.
4. **Learn** — 💡 progressive hints, 🔍 AI code review (rubric: correctness, design, error handling, readability,
   security…), and the editorial + reference unlock after you solve it or give up.

**Ratings** are Elo-style per track (start 1200; Newbie → Pupil → Specialist → Expert → …). Only the first
resolution of each challenge counts: Accepted scores 1 minus 0.1 per wrong submission and 0.05 per hint (min 0.5);
giving up scores 0. Solved challenges appear in the graph (⬢) with `practices` links to their concepts.

Files: `challenges/<slug>/` (yours, committed) · `challenges/history.json` (rating history) ·
`vault/challenges/<slug>.md` (graph node) · `.kg/challenges/<slug>/` (hidden tests, reference, builds — gitignored).
C/C++ are compiled with the first working compiler found (clang/gcc, `-static`). Judged programs run with a minimal
environment (no tokens), in a temp directory, with time limits; the AI-written reference is screened for unsafe APIs
(subprocess, deletes, eval…) before it runs.

### Designed from learning science

The UI follows evidence-based learning research (sources: Dunlosky et al. 2013, Roediger & Karpicke 2006,
Cepeda et al. 2006, Butterfield & Metcalfe 2001, Hattie & Timperley 2007, Chi et al. 1994, Rohrer & Taylor 2007,
Sweller's cognitive-load work, Shneiderman's visualisation mantra, NN/g, WCAG 2.2):

| Principle | Where you see it |
|---|---|
| Retrieval practice + spacing | **Today** shows one next-best action: a time-boxed session (5/10/20 min) built from reviews due + one new concept |
| Interleaving | Review sessions rotate through several concepts instead of blocking one topic |
| Metacognitive calibration | Every MCQ asks *Guessing / Think so / Sure* **before** the answer is revealed; confident errors are flagged as top priority (hypercorrection) and the report shows a calibration table |
| Elaborated, task-focused feedback | Verdict → misconception → why your option is wrong → why the right one is right → reasoning score + what was missing → "explain every option" → your reasoning vs. the worked explanation |
| Self-explanation | Required "why this option — and why not the others?" box (type or 🎤 dictate) |
| Mastery learning | Concepts show 🔒 locked (which prereqs are missing) / 🌱 ready / ✅ mastered, plus "to reach the next level" criteria |
| Overview → zoom → details on demand | Selecting a concept dims everything except its prerequisite chain (highlighted) and what it unlocks; **Highlight** filter for ready / due / gaps / mastered / practised |
| Cognitive load | One question per card, details behind expanders, **Focus mode** hides the graph during a quiz |
| Motivation without dark patterns | Gentle 4-week activity grid (rest days are fine, no streak shaming), domain progress with end-points, a calm "🎉 Mastered" toast |
| Responsiveness | Named loading stages, elapsed time, **Cancel** for long waits; follow-up chips (*Explain simpler*, *Another example*, *Improve my reasoning*, *💡 Hint*) |
| Accessibility | WCAG-contrast light/dark/auto themes, visible focus rings, ≥32 px targets, `aria-live` feedback, colour-blind-safe Okabe–Ito palette + shapes/pips/dashed rings, `prefers-reduced-motion`, keyboard shortcuts (`1`–`4`, `Ctrl+Enter`, `/`, `Esc`, `?`) that can be switched off |

**Look & feel:** a calm "ink" design — near-black neutrals, a warm gold accent, serif headings, small monospace
uppercase labels. Questions are *prediction cards*: bordered options (plus **I don't know**), a **confidence slider**
set before the answer is revealed, and a **Your thinking** box (`Enter` submits). The graph is a dim starfield with soft
domain glows; labels appear as you zoom in or hover. Selecting a concept shows a **lesson map** of its prerequisites
(green = mastered, amber = in progress, red = to learn). Pick a theme with the button in the corner: Auto, Ink, Paper,
Mocha, Rosé Pine or Gruvbox (tokens live in `tools/common.css`).

**Layout:** on the Graph page drag the edge of the left (concepts) or right (details/tutor) panel to resize it, click
the ‹ › button on the edge (or press `[` / `]`) to hide/show it, double-click an edge (or `Home`) to reset. Sizes are
remembered. While a quiz question is open only its own 🎤 button is shown.

### Assessment flow

A chat panel next to the graph, powered by **GitHub Copilot via the Copilot SDK** (your Copilot subscription —
no API keys). Click a concept → **Assess my knowledge**, or type any topic:

1. **Adaptive MCQs** — one question at a time. You pick an option **and explain your reasoning**; the tutor
   grades the reasoning (0–2), names misconceptions, and adapts the next question (harder after strong answers,
   prerequisite probes after weak ones). Correct answers never reach the browser until you've answered.
2. **Report + study plan** — level, strengths, gaps, suggested mastery per concept, and an ordered plan of
   reading (from *your* PDFs, found with full-text search), practice and build exercises with time estimates.
   Book citations are checked against actual search hits and badged *verified* / *unverified* / *external*.
3. **Apply** suggested ratings to the graph with one click; follow-up chat keeps the whole assessment as context.

Plans are saved to `plans/YYYY-MM-DD-<topic>.md` and logged in `journal/`. Configure the model, question count
and port under `tutor:` in `kg.config.yaml`. The server binds to `127.0.0.1` only and rejects cross-site requests.

**🎤 Voice dictation:** press the mic button (or **Ctrl+Space** in a text box) to dictate your reasoning or chat
messages. It uses the browser's Web Speech API — works in Microsoft Edge and Google Chrome (audio is processed by
the browser vendor's cloud speech service). Set the language with `tutor.speech_lang` (e.g. `en-IN`, `hi-IN`).

## 🛠 Build page (`/build`) — projects & open source

Real learning happens by building. The Build page (link at the top of the graph) has three parts:

- **📊 My work** — **Sync from GitHub** pulls the public repos, PRs and issues of your personal account
  (`github.username`) into the vault: repos → `vault/projects/gh-*.md` (type `project`), PRs/issues →
  `vault/contributions/*.md` (type `contribution`). Copilot tags each with the concepts it exercises, creating
  `practices` edges, so concepts show *"Practiced in projects"* in the graph. Re-syncing only updates state
  (merged/closed, stars, last push) and never overwrites your *What I learned* notes.
- **🔎 Find issues** — finds healthy repos (by language, stars, recent activity and good-first-issue/help-wanted
  counts) and their open, unassigned issues, then Copilot ranks them for *you* (fit ★, difficulty, what you'll
  learn, first steps). **Track** saves one as a `target` contribution with a milestone checklist.
- **💡 Project ideas** — Copilot designs projects around concepts you're ready for or weak in (and recent
  assessment gaps), with testable milestones and an open-source angle. **Add** creates `vault/projects/proj-*.md`;
  tick milestones, change status (idea → active → done) and link the repo from the page.

> Your work account is an Enterprise Managed User and can't contribute to public repos, so contributions are
> tracked for your personal account via read-only public API calls (authenticated with your `gh` login for rate
> limits). Configure languages and minimum stars under `github:` in `kg.config.yaml`.

## How it works

```
 PDFs (your books folder) ──kg ingest──▶ .kg/index.db (full-text, per page)
                                     │                 ▲
                                     ▼                 │ kg search
 vault/books/book-*.md ──covers──▶ vault/<domain>/<concept>.md ◀──prereqs── …
                                     │
                         kg build ───▶ build/graph.html (interactive)  ·  kg next / review / rate
```

- **Vault** (`vault/`): one Markdown note per concept with YAML frontmatter (`domain`, `prereqs`, `related`,
  `sources`, `mastery`, `last_reviewed`). Filenames are slugs; `[[slug]]` links work in Obsidian too —
  open `vault/` as an Obsidian vault if you like its graph view and editor.
- **Roadmaps** (`vault/roadmaps/`): staged paths for Math, Systems, Security and AI.
- **Books**: drop PDFs into the folder in `kg.config.yaml` and run `.\kg.cmd ingest`. Each PDF gets a book note
  with its chapter outline, and every page becomes searchable. PDFs and extracted text never enter Git.
- **Mastery** (0–5) drives `kg next` (concepts whose prereqs are ≥ 3) and `kg review` (spaced repetition:
  1d, 3d, 7d, 21d, 60d for mastery 1–5).
- **Copilot**: `.github/copilot-instructions.md` defines the tutor behaviour; skills in `.github/skills/`
  (`teach`, `study-book`) define the session workflows.
- **Validation**: `setup.ps1` installs a pre-commit hook (`.githooks/pre-commit`) that runs `kg check`.

## Commands

| Command | What it does |
|---|---|
| `kg serve [--open] [--model ID] [--port N]` | Graph viewer + AI tutor web app |
| `kg judge <slug> [--submit]` | Judge a development challenge (public tests, or all + rating) |
| `kg next [--domain ai] [-n 10]` | Concepts ready to learn |
| `kg review` | Concepts due for spaced review |
| `kg rate <slug> <0-5>` | Record mastery, stamp `last_reviewed` |
| `kg search "<query>" [--kind note\|book] [--ref slug] [--json]` | Full-text search (BM25) over notes + book pages |
| `kg ingest [--force] [--books-dir DIR]` | Index new/changed PDFs, create book notes |
| `kg new <domain> "<Title>" --prereqs a,b --summary "…"` | Scaffold a concept |
| `kg check [--warnings]` | Validate: missing links, prereq cycles, bad fields |
| `kg build [--open]` | Generate `build/graph.html` + `build/graph.json` |
| `kg progress` | Mastery per domain |

**Mastery scale:** 0 not started · 1 heard of it · 2 can explain basics · 3 can apply it ·
4 can implement/teach it · 5 expert.

## Notes on PDFs

- Scanned PDFs have no text layer; `kg ingest` warns about them. OCR first (e.g. `ocrmypdf in.pdf out.pdf`).
- OneDrive "online-only" files are downloaded on first read; large libraries take a while on the first ingest.
  Re-runs skip unchanged files (SHA-1).

## Ideas for later

- Semantic (embedding) search alongside BM25, and an MCP server exposing `search`/`next`/`rate` to any agent.
- External connectors (paper search, course platforms, flashcard export to Anki).
