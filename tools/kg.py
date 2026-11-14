#!/usr/bin/env python3
"""kg - knowledge-graph toolkit.

Commands:
  check      validate the vault (broken links, prereq cycles, bad fields)
  build      write build/graph.json and build/graph.html
  ingest     extract text/outlines from PDFs in books_dir, create book notes, index pages
  search     full-text search over notes and book pages
  progress   mastery summary per domain
  next       concepts you are ready to learn (all prereqs mastered)
  review     concepts due for spaced review
  rate       set mastery (0-5) for a note and stamp last_reviewed
  new        scaffold a new concept note
  serve      run the graph viewer + AI tutor (MCQ assessment, study plans) on localhost (--provider to switch AI backend)
  judge      judge your solution to a development challenge
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "kg.config.yaml"
INDEX_PATH = ROOT / ".kg" / "index.db"
BUILD_DIR = ROOT / "build"
TEMPLATE_PATH = Path(__file__).resolve().parent / "graph_template.html"

DOMAINS = ["math", "cs", "systems", "security", "ai", "meta"]
TYPES = ["concept", "roadmap", "book", "project", "contribution", "challenge"]
EDGE_KEYS = ["prereqs", "related", "covers", "practices"]
READY_MASTERY = 3
REVIEW_INTERVAL_DAYS = {1: 1, 2: 3, 3: 7, 4: 21, 5: 60}

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- vault

def load_settings() -> dict:
    if CONFIG_PATH.exists():
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8-sig")) or {}
    return {}


def load_config() -> tuple[Path, Path]:
    cfg = load_settings()
    vault = ROOT / cfg.get("vault_dir", "vault")
    books = Path(os.environ.get("KG_BOOKS_DIR") or cfg.get("books_dir") or ROOT / "books")
    return vault, books


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


@dataclass
class Note:
    slug: str
    path: Path
    meta: dict
    body: str

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.slug)

    @property
    def type(self) -> str:
        return str(self.meta.get("type") or "concept")

    @property
    def domain(self) -> str:
        return str(self.meta.get("domain") or "meta")

    @property
    def mastery(self) -> int:
        try:
            return int(self.meta.get("mastery") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def last_reviewed(self) -> dt.date | None:
        value = self.meta.get("last_reviewed")
        if isinstance(value, dt.date):
            return value
        try:
            return dt.date.fromisoformat(str(value)) if value else None
        except ValueError:
            return None

    def refs(self, key: str) -> list[str]:
        value = self.meta.get(key) or []
        if isinstance(value, str):
            value = [value]
        return [str(v).strip().removeprefix("[[").removesuffix("]]") for v in value]

    @property
    def mentions(self) -> list[str]:
        return sorted({m.strip() for m in WIKILINK.findall(self.body)})

    @property
    def summary(self) -> str:
        for line in self.body.splitlines():
            if line.startswith("> "):
                return line[2:].strip()
        return ""


def parse_note(path: Path) -> Note:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    meta, body = {}, text
    if match:
        meta = yaml.safe_load(match.group(1)) or {}
        body = text[match.end():]
    return Note(slug=path.stem, path=path, meta=meta, body=body)


def load_vault(vault: Path) -> tuple[dict[str, Note], list[str]]:
    notes: dict[str, Note] = {}
    errors: list[str] = []
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if any(part.startswith((".", "_")) for part in rel.parts):
            continue
        try:
            note = parse_note(path)
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: invalid frontmatter ({exc.__class__.__name__})")
            continue
        if note.slug in notes:
            errors.append(f"{rel}: duplicate slug '{note.slug}' (also {notes[note.slug].path.relative_to(vault)})")
            continue
        notes[note.slug] = note
    return notes, errors


def find_cycles(notes: dict[str, Note]) -> list[list[str]]:
    WHITE, GREY, BLACK = 0, 1, 2
    color = {slug: WHITE for slug in notes}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(slug: str) -> None:
        color[slug] = GREY
        stack.append(slug)
        for dep in notes[slug].refs("prereqs"):
            if dep not in notes:
                continue
            if color[dep] == GREY:
                cycles.append(stack[stack.index(dep):] + [dep])
            elif color[dep] == WHITE:
                visit(dep)
        stack.pop()
        color[slug] = BLACK

    sys.setrecursionlimit(max(10000, len(notes) * 4))
    for slug in notes:
        if color[slug] == WHITE:
            visit(slug)
    return cycles


def validate(notes: dict[str, Note], vault: Path) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    for slug, note in notes.items():
        rel = note.path.relative_to(vault)
        if note.domain not in DOMAINS:
            errors.append(f"{rel}: unknown domain '{note.domain}' (use one of {DOMAINS})")
        if note.type not in TYPES:
            errors.append(f"{rel}: unknown type '{note.type}' (use one of {TYPES})")
        if not 0 <= note.mastery <= 5:
            errors.append(f"{rel}: mastery must be 0-5")
        for key in EDGE_KEYS:
            for ref in note.refs(key):
                if ref not in notes:
                    errors.append(f"{rel}: {key} -> missing note '{ref}'")
                elif ref == slug:
                    errors.append(f"{rel}: {key} references itself")
        for ref in note.mentions:
            if ref not in notes:
                warnings.append(f"{rel}: unresolved link [[{ref}]]")
    for cycle in find_cycles(notes):
        errors.append("prereq cycle: " + " -> ".join(cycle))
    return errors, warnings


def depth_map(notes: dict[str, Note]) -> dict[str, int]:
    """Longest prereq chain below each note (0 = no prereqs)."""
    memo: dict[str, int] = {}

    def depth(slug: str, seen: frozenset) -> int:
        if slug in memo:
            return memo[slug]
        deps = [d for d in notes[slug].refs("prereqs") if d in notes and d not in seen]
        memo[slug] = 1 + max((depth(d, seen | {d}) for d in deps), default=-1)
        return memo[slug]

    for slug in notes:
        depth(slug, frozenset({slug}))
    return memo


def require_vault() -> tuple[Path, Path, dict[str, Note]]:
    vault, books = load_config()
    notes, errors = load_vault(vault)
    for err in errors:
        print(f"error: {err}", file=sys.stderr)
    return vault, books, notes


# --------------------------------------------------------------------------- check / build

def cmd_check(args) -> int:
    vault, _ = load_config()
    notes, load_errors = load_vault(vault)
    errors, warnings = validate(notes, vault)
    errors = load_errors + errors
    if args.warnings:
        for w in warnings:
            print(f"warn:  {w}")
    for e in errors:
        print(f"error: {e}")
    hint = " (use --warnings to list)" if warnings and not args.warnings else ""
    print(f"{len(notes)} notes, {len(errors)} errors, {len(warnings)} warnings{hint}")
    return 1 if errors else 0


def graph_data(notes: dict[str, Note]) -> dict:
    depths = depth_map(notes)
    due = {n.slug: d for d, n in due_reviews(notes)}
    nodes, edges = [], []
    # Prereq edges win over related/mention edges between the same pair of notes.
    seen = {frozenset((ref, slug)) for slug, n in notes.items() for ref in n.refs("prereqs") if ref in notes}
    for slug, note in notes.items():
        nodes.append({
            "id": slug, "title": note.title, "domain": note.domain, "type": note.type,
            "mastery": note.mastery, "summary": note.summary, "depth": depths.get(slug, 0),
            "path": note.path.relative_to(ROOT).as_posix(),
            "tags": [str(t) for t in note.meta.get("tags") or []],
            "status": str(note.meta.get("status") or ""),
            "url": str(note.meta.get("url") or note.meta.get("repo") or ""),
            "due": slug in due,
        })
        for key, kind in (("prereqs", "prereq"), ("related", "related"), ("covers", "covers"),
                          ("practices", "practices")):
            for ref in note.refs(key):
                if ref not in notes:
                    continue
                src, dst = (ref, slug) if kind == "prereq" else (slug, ref)
                pair = frozenset((src, dst))
                if (pair, kind) in seen or (kind != "prereq" and pair in seen):
                    continue
                seen.update({(pair, kind), pair})
                edges.append({"from": src, "to": dst, "kind": kind})
    for slug, note in notes.items():
        for ref in note.mentions:
            pair = frozenset((slug, ref))
            if ref in notes and ref != slug and pair not in seen:
                seen.add(pair)
                edges.append({"from": slug, "to": ref, "kind": "mention"})
    return {"generated": dt.datetime.now().isoformat(timespec="seconds"), "domains": DOMAINS,
            "nodes": nodes, "edges": edges}


def render_page(template: Path, replacements: dict[str, str] | None = None) -> str:
    """Load an HTML template and inline the shared design system (common.css / common.js)."""
    tools = Path(__file__).resolve().parent
    html = template.read_text(encoding="utf-8")
    html = html.replace("/*__COMMON_CSS__*/", (tools / "common.css").read_text(encoding="utf-8"))
    html = html.replace("/*__COMMON_JS__*/", (tools / "common.js").read_text(encoding="utf-8"))
    for key, value in (replacements or {}).items():
        html = html.replace(key, value)
    return html


def render_graph_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    return render_page(TEMPLATE_PATH, {"__GRAPH_JSON__": payload.replace("</", "<\\/")})


def cmd_build(args) -> int:
    vault, _, notes = require_vault()
    errors, _ = validate(notes, vault)
    if errors:
        print(f"{len(errors)} validation errors; run `kg check`. Building anyway.", file=sys.stderr)
    data = graph_data(notes)
    BUILD_DIR.mkdir(exist_ok=True)
    (BUILD_DIR / "graph.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    out = BUILD_DIR / "graph.html"
    out.write_text(render_graph_html(data), encoding="utf-8")
    print(f"built {out.relative_to(ROOT)}: {len(data['nodes'])} nodes, {len(data['edges'])} edges")
    if args.open:
        if hasattr(os, "startfile"):
            os.startfile(out)
        else:
            print(out.as_uri())
    return 0


# --------------------------------------------------------------------------- index / search

def open_index() -> sqlite3.Connection:
    INDEX_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(INDEX_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, sha1 TEXT, slug TEXT, pages INTEGER);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            kind UNINDEXED, ref UNINDEXED, page UNINDEXED, title, body,
            tokenize='porter unicode61');
    """)
    return db


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def outline_entries(reader, items=None, level=1, max_level=2) -> list[tuple[int, str, int | None]]:
    out: list[tuple[int, str, int | None]] = []
    items = reader.outline if items is None else items
    for item in items:
        if isinstance(item, list):
            if level < max_level:
                out.extend(outline_entries(reader, item, level + 1, max_level))
            continue
        try:
            page = reader.get_destination_page_number(item) + 1
        except Exception:
            page = None
        title = re.sub(r"\s+", " ", str(getattr(item, "title", ""))).strip()
        if title:
            out.append((level, title, page))
    return out


def book_note_text(slug, title, author, rel_pdf, pages, outline) -> str:
    meta = {"title": title, "type": "book", "domain": "meta", "author": author or "unknown",
            "pdf": rel_pdf, "pages": pages, "tags": ["book"], "covers": [], "mastery": 0}
    lines = ["---", yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10000).strip(), "---",
             f"# {title}", "", f"> {author or 'Unknown author'} — {pages} pages. PDF: `{rel_pdf}`", "",
             "Set `domain` above (ai/security/systems/math/cs) and list the concept slugs this book",
             f"teaches in `covers`. Search its text with `kg search \"...\" --ref {slug}`.", "",
             "## Chapters", ""]
    if outline:
        for level, chapter, page in outline:
            suffix = f" (p. {page})" if page else ""
            lines.append(f"{'  ' * (level - 1)}- [ ] {chapter}{suffix}")
    else:
        lines.append("_No PDF outline found — add chapters manually._")
    lines += ["", "## Key takeaways", "", "_Your own words, chapter by chapter._", ""]
    return "\n".join(lines)


def cmd_ingest(args) -> int:
    try:
        from pypdf import PdfReader
    except ImportError:
        print("pypdf missing: run setup.ps1 (or pip install -r requirements.txt)", file=sys.stderr)
        return 2
    import logging
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    vault, books_dir, notes = require_vault()
    books_dir = Path(args.books_dir) if args.books_dir else books_dir
    if not books_dir.is_dir():
        print(f"books dir not found: {books_dir}", file=sys.stderr)
        return 2
    pdfs = sorted(books_dir.rglob("*.pdf"))
    db = open_index()
    book_dir = vault / "books"
    book_dir.mkdir(parents=True, exist_ok=True)
    seen_paths = set()
    for pdf in pdfs:
        rel_pdf = pdf.relative_to(books_dir).as_posix()
        seen_paths.add(rel_pdf)
        digest = sha1_of(pdf)
        row = db.execute("SELECT sha1 FROM files WHERE path=?", (rel_pdf,)).fetchone()
        if row and row[0] == digest and not args.force:
            print(f"= {rel_pdf} (unchanged)")
            continue
        slug = "book-" + slugify(pdf.stem)[:60]
        try:
            reader = PdfReader(str(pdf))
            if reader.is_encrypted:
                reader.decrypt("")
            info = reader.metadata or {}
            title = str(info.get("/Title") or "").strip() or pdf.stem
            author = str(info.get("/Author") or "").strip()
            try:
                outline = outline_entries(reader)
            except Exception:
                outline = []
            db.execute("DELETE FROM chunks WHERE kind='book' AND ref=?", (slug,))
            empty, total = 0, len(reader.pages)
            for i, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                text = re.sub(r"[ \t]+", " ", text).strip()
                if len(text) < 20:
                    empty += 1
                    continue
                db.execute("INSERT INTO chunks(kind, ref, page, title, body) VALUES ('book',?,?,?,?)",
                           (slug, i, title, text))
                if i % 100 == 0:
                    print(f"  … {rel_pdf}: {i}/{total} pages", flush=True)
        except Exception as exc:
            db.rollback()
            print(f"! {rel_pdf}: failed to read ({exc})")
            continue
        db.execute("INSERT OR REPLACE INTO files(path, sha1, slug, pages) VALUES (?,?,?,?)",
                   (rel_pdf, digest, slug, total))
        db.commit()
        note_path = book_dir / f"{slug}.md"
        created = False
        if slug not in notes and not note_path.exists():
            note_path.write_text(book_note_text(slug, title, author, rel_pdf, total, outline), encoding="utf-8")
            created = True
        flag = "  ⚠ little/no text layer — scanned PDF? OCR it first (e.g. ocrmypdf)" if empty > total * 0.8 else ""
        print(f"+ {rel_pdf} -> {slug}: {total - empty}/{total} pages indexed"
              f"{', note created' if created else ''}{flag}")
    for path, slug in db.execute("SELECT path, slug FROM files").fetchall():
        if path not in seen_paths:
            db.execute("DELETE FROM chunks WHERE kind='book' AND ref=?", (slug,))
            db.execute("DELETE FROM files WHERE path=?", (path,))
            print(f"- {path} removed from index (PDF no longer present; note kept)")
    db.commit()
    if not pdfs:
        print(f"no PDFs in {books_dir}")
    return 0


def refresh_note_index(db: sqlite3.Connection, notes: dict[str, Note]) -> None:
    db.execute("DELETE FROM chunks WHERE kind='note'")
    db.executemany("INSERT INTO chunks(kind, ref, page, title, body) VALUES ('note',?,NULL,?,?)",
                   [(s, n.title, n.body) for s, n in notes.items()])
    db.commit()


def fts_query(text: str) -> str:
    return " ".join(f'"{t}"' for t in re.findall(r"\w+", text, re.UNICODE))


def search(notes: dict[str, Note], query: str, kind: str | None = None, ref: str | None = None,
           limit: int = 10, raw: bool = False, any_term: bool = False) -> list[dict]:
    """BM25 search over notes and indexed book pages. Raises ValueError on an empty/invalid query."""
    db = open_index()
    try:
        refresh_note_index(db, notes)
        tokens = fts_query(query)
        match = query if raw else (tokens.replace('" "', '" OR "') if any_term else tokens)
        if not match:
            raise ValueError("empty query")
        sql = ("SELECT kind, ref, page, title, snippet(chunks, 4, '«', '»', ' … ', 18) "
               "FROM chunks WHERE chunks MATCH ?")
        params: list = [match]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if ref:
            sql += " AND ref=?"
            params.append(ref)
        sql += " ORDER BY bm25(chunks, 0, 0, 0, 5.0, 1.0) LIMIT ?"
        params.append(limit)
        try:
            rows = db.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError(f"bad query: {exc}") from exc
    finally:
        db.close()
    return [{"kind": k, "ref": r, "page": p, "title": t, "snippet": re.sub(r"\s+", " ", s)}
            for k, r, p, t, s in rows]


def cmd_search(args) -> int:
    _, _, notes = require_vault()
    try:
        results = search(notes, " ".join(args.query), args.kind, args.ref, args.limit, args.raw)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return 0
    if not results:
        print("no matches")
    for res in results:
        loc = f"{res['ref']} p.{res['page']}" if res["page"] else res["ref"]
        print(f"[{res['kind']}] {loc} — {res['title']}\n    {res['snippet']}\n")
    return 0


# --------------------------------------------------------------------------- learning

def concepts(notes: dict[str, Note], domain: str | None = None) -> list[Note]:
    return [n for n in notes.values() if n.type == "concept" and (not domain or n.domain == domain)]


def bar(value: float, width: int = 20) -> str:
    filled = round(value / 5 * width)
    return "█" * filled + "░" * (width - filled)


def cmd_progress(args) -> int:
    _, _, notes = require_vault()
    print(f"{'domain':<10} {'concepts':>8} {'≥' + str(READY_MASTERY):>5}  avg mastery")
    for domain in DOMAINS:
        items = concepts(notes, domain)
        if not items:
            continue
        avg = sum(n.mastery for n in items) / len(items)
        done = sum(n.mastery >= READY_MASTERY for n in items)
        print(f"{domain:<10} {len(items):>8} {done:>5}  {bar(avg)} {avg:.1f}/5")
    books = [n for n in notes.values() if n.type == "book"]
    print(f"\nbooks: {len(books)} ingested")
    return 0


def ready_concepts(notes: dict[str, Note], domain: str | None) -> list[Note]:
    depths = depth_map(notes)
    ready = [n for n in concepts(notes, domain)
             if n.mastery < READY_MASTERY
             and all(notes[d].mastery >= READY_MASTERY for d in n.refs("prereqs") if d in notes)]
    return sorted(ready, key=lambda n: (-n.mastery, depths.get(n.slug, 0), n.domain, n.slug))


def cmd_next(args) -> int:
    _, _, notes = require_vault()
    ready = ready_concepts(notes, args.domain)[: args.n]
    if not ready:
        print("nothing ready — everything mastered, or check `kg progress`")
    for n in ready:
        prereqs = ", ".join(n.refs("prereqs")) or "none"
        print(f"{n.slug:<36} [{n.domain}] mastery {n.mastery}  (prereqs: {prereqs})\n    {n.summary}")
    return 0


def due_reviews(notes: dict[str, Note]) -> list[tuple[int, Note]]:
    """(days overdue, note) for concepts due for spaced review, most overdue first."""
    today = dt.date.today()
    due = []
    for n in concepts(notes):
        if n.mastery < 1:
            continue
        last = n.last_reviewed
        due_on = last + dt.timedelta(days=REVIEW_INTERVAL_DAYS[n.mastery]) if last else today
        if due_on <= today:
            due.append(((today - due_on).days, n))
    due.sort(key=lambda x: (-x[0], x[1].mastery))
    return due


def cmd_review(args) -> int:
    _, _, notes = require_vault()
    due = due_reviews(notes)
    if not due:
        print("nothing due for review today")
    for overdue, n in due[: args.n]:
        print(f"{n.slug:<36} [{n.domain}] mastery {n.mastery}  overdue {overdue}d")
    return 0


def set_frontmatter_field(text: str, key: str, value: str) -> str:
    """Replace (or append) a top-level frontmatter key, including any wrapped/indented or block-list
    continuation lines that belong to its old value."""
    match = FRONTMATTER.match(text)
    if not match:
        return f"---\n{key}: {value}\n---\n" + text
    lines = match.group(1).split("\n")
    out, i, done = [], 0, False
    while i < len(lines):
        if not done and re.match(rf"^{re.escape(key)}:", lines[i]):
            out.append(f"{key}: {value}")
            i += 1
            while i < len(lines) and (lines[i][:1] in (" ", "\t") or lines[i].startswith("- ")):
                i += 1
            done = True
            continue
        out.append(lines[i])
        i += 1
    if not done:
        out.append(f"{key}: {value}")
    return text[: match.start(1)] + "\n".join(out) + text[match.end(1):]


def rate_note(note: Note, mastery: int) -> None:
    text = note.path.read_text(encoding="utf-8")
    text = set_frontmatter_field(text, "mastery", str(mastery))
    text = set_frontmatter_field(text, "last_reviewed", dt.date.today().isoformat())
    note.path.write_text(text, encoding="utf-8")


def cmd_rate(args) -> int:
    _, _, notes = require_vault()
    note = notes.get(args.slug)
    if not note:
        print(f"no note '{args.slug}'", file=sys.stderr)
        return 2
    rate_note(note, args.mastery)
    nxt = REVIEW_INTERVAL_DAYS.get(args.mastery)
    print(f"{args.slug}: mastery {note.mastery} -> {args.mastery}"
          + (f"; next review in {nxt}d" if nxt else ""))
    return 0


def cmd_new(args) -> int:
    vault, _, notes = require_vault()
    slug = args.slug or slugify(args.title)
    if slug in notes:
        print(f"note '{slug}' already exists: {notes[slug].path}", file=sys.stderr)
        return 2
    prereqs = [p.strip() for p in (args.prereqs or "").split(",") if p.strip()]
    missing = [p for p in prereqs if p not in notes]
    if missing:
        print(f"warning: prereqs not in vault yet: {missing}", file=sys.stderr)
    meta = {"title": args.title, "type": "concept", "domain": args.domain, "tags": [],
            "prereqs": prereqs, "related": [], "sources": [], "mastery": 0, "last_reviewed": None}
    body = (f"# {args.title}\n\n> {args.summary or 'One-sentence summary.'}\n\n"
            "## Key ideas\n\n- \n\n## Check yourself\n\n1. \n\n## My notes\n\n"
            "_Explain it in your own words after each session._\n")
    path = vault / args.domain / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10000).strip()
                    + "\n---\n" + body, encoding="utf-8")
    print(f"created {path.relative_to(ROOT)}")
    return 0


def cmd_judge(args) -> int:
    import arena
    return arena.cli_judge(args.slug, args.submit)


def cmd_serve(args) -> int:
    try:
        import server
    except ImportError as exc:
        print(f"missing dependency ({exc.name}): run setup.ps1", file=sys.stderr)
        return 2
    return server.run(port=args.port, model=args.model, open_browser=args.open, provider=args.provider)


# --------------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kg", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="validate the vault")
    p.add_argument("--warnings", action="store_true", help="list unresolved [[links]] too")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("build", help="build graph.json + interactive graph.html")
    p.add_argument("--open", action="store_true", help="open graph.html afterwards")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("ingest", help="index PDFs from books_dir and create book notes")
    p.add_argument("--books-dir", help="override books_dir from kg.config.yaml")
    p.add_argument("--force", action="store_true", help="re-index unchanged PDFs")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("search", help="full-text search notes and books")
    p.add_argument("query", nargs="+")
    p.add_argument("--kind", choices=["note", "book"])
    p.add_argument("--ref", help="restrict to one note/book slug")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--raw", action="store_true", help="pass query as raw FTS5 syntax")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("progress", help="mastery per domain")
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser("next", help="concepts whose prereqs are mastered")
    p.add_argument("--domain", choices=DOMAINS)
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("review", help="concepts due for spaced review")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("rate", help="set mastery 0-5 and stamp last_reviewed")
    p.add_argument("slug")
    p.add_argument("mastery", type=int, choices=range(6))
    p.set_defaults(func=cmd_rate)

    p = sub.add_parser("new", help="scaffold a concept note")
    p.add_argument("domain", choices=DOMAINS)
    p.add_argument("title")
    p.add_argument("--slug")
    p.add_argument("--prereqs", help="comma-separated slugs")
    p.add_argument("--summary")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("judge", help="judge a development challenge (public tests; --submit for all + rating)")
    p.add_argument("slug", help="challenge slug, e.g. ch-log-summariser")
    p.add_argument("--submit", action="store_true", help="run hidden tests too and record the result")
    p.set_defaults(func=cmd_judge)

    p = sub.add_parser("serve", help="run the graph + AI tutor web app")
    p.add_argument("--port", type=int)
    p.add_argument("--provider", choices=["claude-code", "copilot", "anthropic", "openai"],
                   help="AI backend (default: tutor.provider in kg.config.yaml)")
    p.add_argument("--model", help="model id for the chosen provider (default: tutor.model in kg.config.yaml)")
    p.add_argument("--open", action="store_true", help="open the browser")
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
