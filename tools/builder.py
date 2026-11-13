"""Build features: GitHub sync (repos → projects, PRs/issues → contributions), open-source issue finder,
and AI project suggestions. All AI calls go through the tutor's Copilot client."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import shutil
import subprocess
from typing import Literal

import aiohttp
import yaml
from pydantic import BaseModel, ConfigDict, Field

import kg
from tutor import Tutor, TutorError

API = "https://api.github.com"
SYNC_STATE = kg.ROOT / ".kg" / "github_sync.json"
LANG_QUALIFIER = {"c++": "cpp", "c#": "csharp", "f#": "fsharp", "objective-c": "objective-c"}
ACTIVE_DAYS = 120


def resolve_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        try:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


class GitHub:
    """Minimal read-only GitHub REST client (public data)."""

    def __init__(self):
        self.token = resolve_token()
        self.session: aiohttp.ClientSession | None = None
        self.sem = asyncio.Semaphore(6)

    async def _http(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                       "User-Agent": "knowledge-graph-tutor"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self.session = aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30))
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def get(self, path: str, params: dict | None = None, raw: bool = False):
        http = await self._http()
        headers = {"Accept": "application/vnd.github.raw"} if raw else None
        async with self.sem, http.get(API + path, params=params, headers=headers) as r:
            if r.status == 404:
                return None
            if r.status in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
                raise TutorError("GitHub API rate limit reached; try again in a minute", 429)
            if r.status >= 400:
                raise TutorError(f"GitHub API {r.status} for {path}: {(await r.text())[:200]}", 502)
            return await r.text() if raw else await r.json()

    async def search_issues(self, q: str, per_page: int = 100, sort: str | None = None) -> list[dict]:
        params = {"q": q, "per_page": per_page}
        if sort:
            params["sort"] = sort
        data = await self.get("/search/issues", params)
        return (data or {}).get("items", [])


# --------------------------------------------------------------------------- schemas

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Domain = Literal["math", "cs", "systems", "security", "ai", "meta"]


class Tag(Strict):
    id: str
    domain: Domain
    concepts: list[str] = Field(description="0-4 concept slugs from the catalog that this work genuinely exercises")
    summary: str = Field(description="one sentence: what was built/changed and the CS skills involved")


class Tags(Strict):
    items: list[Tag]


class IssuePick(Strict):
    idx: int = Field(description="candidate index")
    fit: int = Field(description="1-5 fit for this learner (skill level, interests, learning value)")
    difficulty: Literal["starter", "moderate", "stretch"]
    why: str = Field(description="why this issue suits the learner now")
    you_will_learn: str
    concepts: list[str] = Field(description="concept slugs from the catalog it exercises")
    first_steps: list[str] = Field(description="3-5 concrete first steps")


class IssuePicks(Strict):
    picks: list[IssuePick]


class Idea(Strict):
    title: str
    pitch: str = Field(description="2-3 sentences: what you build and why it teaches the target concepts")
    domain: Domain
    concepts: list[str] = Field(description="concept slugs from the catalog")
    difficulty: Literal["beginner", "intermediate", "advanced"]
    est_hours: float
    stack: list[str]
    milestones: list[str] = Field(description="4-8 ordered, testable milestones")
    stretch: list[str]
    open_source_angle: str = Field(description="how to publish it or which real OSS project it relates to")


class Ideas(Strict):
    ideas: list[Idea]


BUILD_SYSTEM = """You are a senior engineer mentoring a learner who studies computer science and maths (focus: AI,
security, systems programming) and learns best by building projects and contributing to open source.
Only use concept slugs that appear in the provided catalog. Be concrete and honest about difficulty."""


# --------------------------------------------------------------------------- helpers

def notes() -> dict[str, kg.Note]:
    vault, _ = kg.load_config()
    return kg.load_vault(vault)[0]


def catalog(n: dict[str, kg.Note]) -> str:
    return "\n".join(f"{s} — {x.title} ({x.domain})" for s, x in n.items() if x.type == "concept")


def latest_gaps(limit: int = 3) -> list[str]:
    plans = sorted((kg.ROOT / "plans").glob("*.md"), reverse=True)[:limit] if (kg.ROOT / "plans").exists() else []
    gaps: list[str] = []
    for p in plans:
        m = re.search(r"^## Gaps\n\n(.*?)(?:\n## |\Z)", p.read_text(encoding="utf-8"), re.S | re.M)
        if m:
            gaps += [g[2:].strip() for g in m.group(1).splitlines() if g.startswith("- ")]
    return gaps[:12]


def learner_profile(n: dict[str, kg.Note]) -> str:
    cs = [x for x in n.values() if x.type == "concept"]
    lines = ["Mastery by domain (avg 0-5): " + ", ".join(
        f"{d}={sum(x.mastery for x in cs if x.domain == d) / max(1, sum(1 for x in cs if x.domain == d)):.1f}"
        for d in kg.DOMAINS if any(x.domain == d for x in cs))]
    strong = [x.slug for x in cs if x.mastery >= 3]
    learning = [x.slug for x in cs if 1 <= x.mastery < 3]
    ready = [x.slug for x in kg.ready_concepts(n, None)[:10]]
    lines += [f"Solid (>=3): {', '.join(strong) or 'none yet'}", f"Learning (1-2): {', '.join(learning) or 'none'}",
              f"Ready to learn next: {', '.join(ready)}"]
    langs: dict[str, int] = {}
    for x in n.values():
        if x.type == "project" and x.meta.get("language"):
            langs[x.meta["language"]] = langs.get(x.meta["language"], 0) + 1
    if langs:
        lines.append("Languages used in own repos: " + ", ".join(f"{k}×{v}" for k, v in sorted(langs.items(), key=lambda kv: -kv[1])))
    projects = [x.title for x in n.values() if x.type == "project"]
    if projects:
        lines.append("Existing projects: " + ", ".join(projects[:40]))
    gaps = latest_gaps()
    if gaps:
        lines.append("Recent assessment gaps: " + " | ".join(gaps))
    return "\n".join(lines)


def write_note(path, meta: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000).strip()
                    + "\n---\n" + body, encoding="utf-8")


def update_fields(note: kg.Note, fields: dict) -> None:
    text = note.path.read_text(encoding="utf-8")
    for k, v in fields.items():
        text = kg.set_frontmatter_field(text, k, yaml.safe_dump(v, default_flow_style=True, width=1000).strip()
                                        .removesuffix("...").strip())
    note.path.write_text(text, encoding="utf-8")


def checklist(body: str, section: str) -> list[dict]:
    items, in_sec = [], False
    for line in body.splitlines():
        if line.startswith("## "):
            in_sec = line[3:].strip().lower() == section.lower()
            continue
        m = re.match(r"\s*- \[([ xX])\] (.*)", line)
        if in_sec and m:
            items.append({"done": m.group(1) != " ", "text": m.group(2).strip()})
    return items


def toggle_checklist(text: str, section: str, index: int, done: bool) -> str:
    lines, in_sec, k = text.split("\n"), False, -1
    for i, line in enumerate(lines):
        if line.startswith("## "):
            in_sec = line[3:].strip().lower() == section.lower()
            continue
        if in_sec and re.match(r"\s*- \[[ xX]\] ", line):
            k += 1
            if k == index:
                lines[i] = re.sub(r"- \[[ xX]\]", "- [x]" if done else "- [ ]", line, count=1)
                return "\n".join(lines)
    raise TutorError("milestone not found")


def clean_concepts(slugs: list[str], n: dict[str, kg.Note]) -> list[str]:
    out = []
    for s in slugs:
        if s in n and n[s].type == "concept" and s not in out:
            out.append(s)
    return out[:5]


def iso_date(value: str | None) -> str | None:
    return value[:10] if value else None


# --------------------------------------------------------------------------- engine

class Builder:
    def __init__(self, tutor: Tutor, cfg: dict):
        self.tutor = tutor
        self.username = cfg.get("username") or ""
        self.languages = cfg.get("languages") or ["Python", "C", "C++", "Rust", "Go"]
        self.min_stars = int(cfg.get("min_stars", 50))
        self.gh = GitHub()
        self.last_issues: list[dict] = []
        self.last_ideas: list[dict] = []

    async def close(self) -> None:
        await self.gh.close()

    # ---- summary

    def summary(self) -> dict:
        n = notes()
        projects, contributions = [], []
        for s, x in n.items():
            if x.type == "project":
                ms = checklist(x.body, "Milestones")
                projects.append({"slug": s, "title": x.title, "summary": x.summary, "domain": x.domain,
                                 "status": x.meta.get("status") or "idea", "source": x.meta.get("source") or "idea",
                                 "url": x.meta.get("url") or x.meta.get("repo") or "", "language": x.meta.get("language"),
                                 "last_push": str(x.meta.get("last_push") or ""), "practices": x.refs("practices"),
                                 "milestones": ms, "est_hours": x.meta.get("est_hours"),
                                 "difficulty": x.meta.get("difficulty")})
            elif x.type == "contribution":
                contributions.append({"slug": s, "title": x.title, "summary": x.summary, "kind": x.meta.get("kind"),
                                      "state": x.meta.get("state"), "repo": x.meta.get("repo"), "url": x.meta.get("url"),
                                      "own_repo": bool(x.meta.get("own_repo")), "created": str(x.meta.get("created") or ""),
                                      "practices": x.refs("practices"), "fit": x.meta.get("fit"),
                                      "milestones": checklist(x.body, "Milestones")})
        projects.sort(key=lambda p: p["last_push"], reverse=True)
        projects.sort(key=lambda p: {"active": 0, "idea": 1, "done": 2}.get(p["status"], 3))
        contributions.sort(key=lambda c: c["created"], reverse=True)
        ext = [c for c in contributions if not c["own_repo"] and c["kind"] in ("pr", "issue")]
        practiced = {s for p in projects for s in p["practices"]} | {s for c in contributions for s in c["practices"]}
        langs: dict[str, int] = {}
        for p in projects:
            if p["language"]:
                langs[p["language"]] = langs.get(p["language"], 0) + 1
        state = json.loads(SYNC_STATE.read_text()) if SYNC_STATE.exists() else {}
        return {
            "username": self.username, "languages": self.languages, "min_stars": self.min_stars,
            "last_sync": state.get("time"), "authenticated": bool(self.gh.token),
            "stats": {"projects": len(projects), "active": sum(p["status"] == "active" for p in projects),
                      "prs": sum(c["kind"] == "pr" for c in ext), "merged": sum(c["state"] == "merged" for c in ext),
                      "issues": sum(c["kind"] == "issue" for c in ext),
                      "targets": sum(c["kind"] == "target" for c in contributions),
                      "concepts_practiced": len(practiced), "languages": langs},
            "projects": projects, "contributions": contributions,
            "concept_titles": {s: n[s].title for s in practiced if s in n},
        }

    # ---- sync

    async def sync(self) -> dict:
        if not self.username:
            raise TutorError("set github.username in kg.config.yaml")
        u = self.username
        repos, prs, issues = await asyncio.gather(
            self.gh.get(f"/users/{u}/repos", {"per_page": 100, "sort": "pushed", "type": "owner"}),
            self.gh.search_issues(f"author:{u} is:pr"),
            self.gh.search_issues(f"author:{u} is:issue"))
        if repos is None:
            raise TutorError(f"GitHub user '{u}' not found", 404)
        n = notes()
        vault, _ = kg.load_config()
        today = dt.date.today()
        new: list[dict] = []
        updated = 0

        for r in (r for r in repos if not r.get("fork")):
            slug = "gh-" + kg.slugify(r["name"])[:60]
            pushed = iso_date(r.get("pushed_at"))
            status = "active" if pushed and (today - dt.date.fromisoformat(pushed)).days <= ACTIVE_DAYS else "done"
            fields = {"stars": r.get("stargazers_count", 0), "last_push": pushed, "language": r.get("language")}
            if slug in n:
                update_fields(n[slug], fields)
                updated += 1
                continue
            new.append({"id": slug, "kind": "repo", "r": r, "status": status, "fields": fields})

        for item in prs + issues:
            owner, repo = item["repository_url"].split("/repos/")[1].split("/")
            is_pr = "pull_request" in item
            slug = f"{'pr' if is_pr else 'issue'}-{kg.slugify(owner)}-{kg.slugify(repo)}-{item['number']}"[:90]
            merged = is_pr and (item.get("pull_request") or {}).get("merged_at")
            state = "merged" if merged else item["state"]
            fields = {"state": state, "closed": iso_date(item.get("closed_at"))}
            if slug in n:
                update_fields(n[slug], fields)
                updated += 1
                continue
            new.append({"id": slug, "kind": "pr" if is_pr else "issue", "item": item, "owner": owner, "repo": repo,
                        "fields": fields})

        async def readme(r):
            try:
                text = await self.gh.get(f"/repos/{r['full_name']}/readme", raw=True)
            except TutorError:
                text = None
            return re.sub(r"\s+", " ", text or "")[:1200]

        repo_items = [x for x in new if x["kind"] == "repo"]
        for x, text in zip(repo_items, await asyncio.gather(*(readme(x["r"]) for x in repo_items))):
            x["readme"] = text

        tags: dict[str, Tag] = {}
        for i in range(0, len(new), 12):
            tags.update(await self._tag(new[i:i + 12], n))

        for x in new:
            t = tags.get(x["id"])
            concepts = clean_concepts(t.concepts, n) if t else []
            summary = (t.summary if t else "") or ""
            domain = t.domain if t else "cs"
            if x["kind"] == "repo":
                r = x["r"]
                meta = {"title": r["name"], "type": "project", "domain": domain, "tags": ["github"],
                        "practices": concepts, "status": x["status"], "source": "github", "url": r["html_url"],
                        **x["fields"]}
                body = (f"# {r['name']}\n\n> {summary or r.get('description') or 'GitHub repository'}\n\n"
                        f"{r.get('description') or ''}\n\nRepo: <{r['html_url']}>\n\n"
                        "## Milestones\n\n## What I learned\n\n_Your reflections: design decisions, bugs, what you'd redo._\n")
                write_note(vault / "projects" / f"{x['id']}.md", meta, body)
            else:
                it = x["item"]
                meta = {"title": it["title"], "type": "contribution", "domain": domain, "kind": x["kind"],
                        "repo": f"{x['owner']}/{x['repo']}", "url": it["html_url"],
                        "own_repo": x["owner"].lower() == self.username.lower(), "created": iso_date(it["created_at"]),
                        "practices": concepts, "tags": ["github"], **x["fields"]}
                body = (f"# {it['title']}\n\n> {summary or it['title']}\n\n[{x['owner']}/{x['repo']}#{it['number']}]"
                        f"({it['html_url']})\n\n## What I learned\n\n_Review feedback, techniques, what surprised you._\n")
                write_note(vault / "contributions" / f"{x['id']}.md", meta, body)

        SYNC_STATE.parent.mkdir(exist_ok=True)
        SYNC_STATE.write_text(json.dumps({"time": dt.datetime.now().isoformat(timespec="seconds")}))
        return {"new": len(new), "updated": updated, **self.summary()}

    async def _tag(self, batch: list[dict], n: dict[str, kg.Note]) -> dict[str, Tag]:
        entries = []
        for x in batch:
            if x["kind"] == "repo":
                r = x["r"]
                entries.append({"id": x["id"], "type": "own repository", "name": r["name"],
                                "description": r.get("description"), "language": r.get("language"),
                                "topics": r.get("topics"), "readme_excerpt": x.get("readme")})
            else:
                it = x["item"]
                entries.append({"id": x["id"], "type": x["kind"], "repo": f"{x['owner']}/{x['repo']}",
                                "title": it["title"], "body_excerpt": re.sub(r"\s+", " ", it.get("body") or "")[:600]})
        prompt = ("Tag each item of the learner's GitHub work with its domain, the concept slugs it genuinely "
                  "exercises (0-4, from the catalog only), and a one-sentence summary. Return one entry per id.\n\n"
                  f"Concept catalog:\n{catalog(n)}\n\nItems:\n{json.dumps(entries, ensure_ascii=False)}")
        result = await self.tutor.ask_typed(BUILD_SYSTEM, prompt, Tags, timeout=300)
        return {t.id: t for t in result.items}

    # ---- open-source issue finder

    async def find_issues(self, languages: list[str], labels: list[str], keywords: str, min_stars: int,
                          limit: int = 8) -> list[dict]:
        languages = [l.strip() for l in languages if l.strip()][:4] or self.languages[:3]
        labels = [l.strip() for l in labels if l.strip()] or ["good first issue"]
        since = (dt.date.today() - dt.timedelta(days=60)).isoformat()
        kw = re.sub(r"[^\w\s\-+#.]", " ", keywords or "").strip()
        qual = "good-first-issues:>2" if "good first issue" in labels else "help-wanted-issues:>2"

        async def repos_for(lang: str) -> list[dict]:
            q = (f"{kw} language:{LANG_QUALIFIER.get(lang.lower(), lang.lower())} stars:>={min_stars} "
                 f"pushed:>={since} archived:false {qual}").strip()
            params = {"q": q, "per_page": 8}
            if not kw:
                params["sort"] = "help-wanted-issues"
            data = await self.gh.get("/search/repositories", params)
            return (data or {}).get("items", [])

        async def issues_for(repo: dict) -> list[tuple[dict, dict]]:
            out = []
            for label in labels:
                items = await self.gh.get(f"/repos/{repo['full_name']}/issues", {
                    "labels": label, "state": "open", "assignee": "none", "sort": "updated", "per_page": 4})
                out += [(it, repo) for it in items or [] if "pull_request" not in it]
            return out

        repos: dict[str, dict] = {}
        for items in await asyncio.gather(*(repos_for(l) for l in languages)):
            for r in items:
                repos.setdefault(r["full_name"], r)
        found: dict[str, tuple[dict, dict]] = {}
        for pairs in await asyncio.gather(*(issues_for(r) for r in list(repos.values())[:24])):
            for it, r in pairs:
                found.setdefault(it["html_url"], (it, r))
        candidates = []
        for it, info in found.values():
            candidates.append({"title": it["title"], "url": it["html_url"], "repo": info["full_name"],
                               "stars": info["stargazers_count"], "language": info.get("language"),
                               "repo_description": info.get("description"), "topics": info.get("topics", [])[:8],
                               "labels": [l["name"] for l in it.get("labels", [])][:6], "comments": it.get("comments", 0),
                               "updated": iso_date(it.get("updated_at")),
                               "body_excerpt": re.sub(r"\s+", " ", it.get("body") or "")[:500]})
        candidates.sort(key=lambda c: c["updated"] or "", reverse=True)
        candidates.sort(key=lambda c: c["comments"] > 8)
        candidates = candidates[:30]
        if not candidates:
            return []
        n = notes()
        prompt = (f"Learner profile:\n{learner_profile(n)}\n\nInterests/keywords: {keywords or '(any CS)'}\n\n"
                  f"Concept catalog:\n{catalog(n)}\n\nCandidate open-source issues (idx: data):\n"
                  + "\n".join(f"{i}: {json.dumps(c, ensure_ascii=False)}" for i, c in enumerate(candidates))
                  + f"\n\nPick the best {limit} for this learner to actually complete and learn from. Prefer "
                    "well-scoped issues in healthy projects; avoid vague, huge or already-claimed-looking ones "
                    "(many comments). Rank best first.")
        picks = await self.tutor.ask_typed(BUILD_SYSTEM, prompt, IssuePicks, timeout=300)
        out = []
        for p in picks.picks:
            if 0 <= p.idx < len(candidates) and all(o["url"] != candidates[p.idx]["url"] for o in out):
                out.append({**candidates[p.idx], **p.model_dump(exclude={"idx"}),
                            "fit": max(1, min(5, p.fit)), "concepts": clean_concepts(p.concepts, n)})
        self.last_issues = out[:limit]
        return self.last_issues

    def track_issue(self, url: str) -> dict:
        pick = next((x for x in self.last_issues if x["url"] == url), None)
        if not pick:
            raise TutorError("issue not in the latest results; search again")
        m = re.match(r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)", url)
        if not m:
            raise TutorError("unexpected issue URL")
        owner, repo, num = m.groups()
        slug = f"target-{kg.slugify(owner)}-{kg.slugify(repo)}-{num}"[:90]
        vault, _ = kg.load_config()
        path = vault / "contributions" / f"{slug}.md"
        if path.exists():
            return {"slug": slug, "existing": True}
        meta = {"title": pick["title"], "type": "contribution", "domain": "cs", "kind": "target", "state": "planned",
                "repo": f"{owner}/{repo}", "url": url, "own_repo": False, "created": dt.date.today().isoformat(),
                "fit": pick["fit"], "practices": pick["concepts"], "tags": ["github", "open-source"]}
        body = (f"# {pick['title']}\n\n> {pick['you_will_learn']}\n\n[{owner}/{repo}#{num}]({url}) · ★{pick['stars']}\n\n"
                f"**Why:** {pick['why']}\n\n## Milestones\n\n"
                + "".join(f"- [ ] {s}\n" for s in pick["first_steps"])
                + "- [ ] Comment on the issue to claim it (from your personal account)\n"
                  "- [ ] Open a pull request\n- [ ] Address review feedback\n\n## What I learned\n\n")
        write_note(path, meta, body)
        return {"slug": slug, "existing": False}

    # ---- projects

    async def suggest_projects(self, focus: str, hours: float, interests: str, count: int = 4) -> list[dict]:
        n = notes()
        focus = focus if focus in kg.DOMAINS else "any"
        prompt = (f"Learner profile:\n{learner_profile(n)}\n\nConcept catalog:\n{catalog(n)}\n\n"
                  f"Suggest {count} hands-on projects. Focus domain: {focus}. Time budget per project: about "
                  f"{hours:g} hours. Extra interests: {interests or 'none given'}. Preferred languages: "
                  f"{', '.join(self.languages)}.\nEach project must exercise concepts the learner is ready for or "
                  "weak in, produce something real (tool, library, service, exploit lab, model) worth publishing on "
                  "GitHub, and avoid duplicating existing projects. Milestones must be testable.")
        ideas = await self.tutor.ask_typed(BUILD_SYSTEM, prompt, Ideas, timeout=300)
        self.last_ideas = [{**i.model_dump(), "concepts": clean_concepts(i.concepts, n)} for i in ideas.ideas]
        return self.last_ideas

    def create_project(self, title: str) -> dict:
        idea = next((i for i in self.last_ideas if i["title"] == title), None)
        if not idea:
            raise TutorError("idea not in the latest suggestions")
        vault, _ = kg.load_config()
        slug = "proj-" + kg.slugify(idea["title"])[:60]
        path = vault / "projects" / f"{slug}.md"
        if path.exists():
            raise TutorError("a project with this name already exists")
        meta = {"title": idea["title"], "type": "project", "domain": idea["domain"], "tags": ["project"],
                "practices": idea["concepts"], "status": "idea", "source": "tutor", "difficulty": idea["difficulty"],
                "est_hours": idea["est_hours"], "stack": idea["stack"], "repo": None}
        body = (f"# {idea['title']}\n\n> {idea['pitch']}\n\n**Stack:** {', '.join(idea['stack'])}\n\n## Milestones\n\n"
                + "".join(f"- [ ] {m}\n" for m in idea["milestones"])
                + "\n## Stretch goals\n\n" + "".join(f"- [ ] {s}\n" for s in idea["stretch"])
                + f"\n## Open-source angle\n\n{idea['open_source_angle']}\n\n## Log\n\n_Notes as you build._\n")
        write_note(path, meta, body)
        return {"slug": slug}

    def update_project(self, slug: str, status: str | None, repo: str | None,
                       milestone: int | None, done: bool | None) -> dict:
        n = notes()
        x = n.get(slug)
        if not x or x.type not in ("project", "contribution"):
            raise TutorError("unknown project", 404)
        if status is not None:
            if status not in ("idea", "active", "done", "planned", "abandoned"):
                raise TutorError("bad status")
            update_fields(x, {"status" if x.type == "project" else "state": status})
        if repo is not None:
            if repo and not re.match(r"https://github\.com/[\w.-]+/[\w.-]+/?$", repo):
                raise TutorError("repo must be a https://github.com/owner/name URL")
            update_fields(x, {"repo": repo or None})
        if milestone is not None:
            x = notes()[slug]
            x.path.write_text(toggle_checklist(x.path.read_text(encoding="utf-8"), "Milestones", milestone, bool(done)),
                              encoding="utf-8")
        return {"ok": True}
