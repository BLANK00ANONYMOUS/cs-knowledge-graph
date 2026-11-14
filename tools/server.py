"""Local web app: live knowledge-graph viewer + AI tutor API (bound to 127.0.0.1)."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import webbrowser

from aiohttp import web

import kg
from arena import Arena
from builder import Builder
from llm import make_provider
from tutor import Tutor, TutorError

DEFAULT_PORT = 8765
BUILD_TEMPLATE = kg.ROOT / "tools" / "build_template.html"
TODAY_TEMPLATE = kg.ROOT / "tools" / "today_template.html"
ARENA_TEMPLATE = kg.ROOT / "tools" / "arena_template.html"


def notes():
    vault, _ = kg.load_config()
    return kg.load_vault(vault)[0]


@web.middleware
async def guard(request: web.Request, handler):
    # Block cross-site requests: POSTs must carry our custom header (forces a CORS preflight we never answer)
    # and, when the browser sends an Origin, it must be this server.
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if request.headers.get("X-KG") != "1" or (origin and origin != f"http://{request.host}"):
            return web.json_response({"error": "forbidden"}, status=403)
    try:
        return await handler(request)
    except TutorError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)
    except asyncio.TimeoutError:
        return web.json_response({"error": "the model took too long; try again"}, status=504)
    except web.HTTPException:
        raise
    except Exception as exc:  # surface model/SDK errors to the UI instead of a bare 500
        msg = f"{type(exc).__name__}: {exc}"
        if "OAuth token" in msg or "HMAC" in msg:
            msg = ("Not signed in to GitHub Copilot. In a terminal run `gh auth login` (use your GitHub account that "
                   "has Copilot), then restart `.\\kg.cmd serve`.")
        return web.json_response({"error": msg}, status=500)


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except json.JSONDecodeError:
        raise TutorError("invalid JSON")
    if not isinstance(data, dict):
        raise TutorError("expected a JSON object")
    return data


def tutor(request: web.Request) -> Tutor:
    return request.app["tutor"]


async def index(request: web.Request) -> web.Response:
    return web.Response(text=kg.render_page(TODAY_TEMPLATE), content_type="text/html")


async def graph_page(request: web.Request) -> web.Response:
    return web.Response(text=kg.render_graph_html(kg.graph_data(notes())), content_type="text/html")


async def health(request: web.Request) -> web.Response:
    n = notes()
    return web.json_response({"ok": True, "model": tutor(request).model, "provider": tutor(request).provider.label,
                              "books": sum(1 for x in n.values() if x.type == "book"),
                              "questions": tutor(request).default_questions,
                              "speech_lang": request.app["speech_lang"]})


async def graph(request: web.Request) -> web.Response:
    return web.json_response(kg.graph_data(notes()))


async def today(request: web.Request) -> web.Response:
    """Next-best-actions for the home view: reviews due, concepts ready, work in progress, recent activity."""
    n = notes()
    title = lambda s: n[s].title if s in n else s
    due = [{"slug": x.slug, "title": x.title, "domain": x.domain, "mastery": x.mastery, "overdue": d}
           for d, x in kg.due_reviews(n)[:8]]
    ready = [{"slug": x.slug, "title": x.title, "domain": x.domain, "mastery": x.mastery, "summary": x.summary,
              "prereqs": [title(p) for p in x.refs("prereqs")]} for x in kg.ready_concepts(n, None)[:6]]
    work = []
    await builder(request).account.ensure()
    summary = builder(request).summary()
    for item in summary["projects"] + summary["contributions"]:
        state = item.get("status") or item.get("state")
        if state not in ("active", "planned"):
            continue
        ms = item.get("milestones") or []
        nxt = next((m["text"] for m in ms if not m["done"]), None)
        work.append({"slug": item["slug"], "title": item["title"], "state": state, "url": item.get("url") or "",
                     "done": sum(m["done"] for m in ms), "total": len(ms), "next": nxt,
                     "kind": "target" if item.get("kind") == "target" else "project"})
    domains = []
    for d in kg.DOMAINS:
        cs = [x for x in n.values() if x.type == "concept" and x.domain == d]
        if cs:
            domains.append({"domain": d, "total": len(cs), "mastered": sum(x.mastery >= kg.READY_MASTERY for x in cs),
                            "avg": round(sum(x.mastery for x in cs) / len(cs), 2)})
    days = {p.stem for p in (kg.ROOT / "journal").glob("????-??-??.md")}
    days |= {x.last_reviewed.isoformat() for x in n.values() if x.last_reviewed}
    start = dt.date.today() - dt.timedelta(days=27)
    activity = [{"date": (start + dt.timedelta(days=i)).isoformat(),
                 "active": (start + dt.timedelta(days=i)).isoformat() in days} for i in range(28)]
    plans = sorted((kg.ROOT / "plans").glob("*.md"), reverse=True) if (kg.ROOT / "plans").exists() else []
    import arena as arena_mod
    rt = arena_mod.ratings()
    open_ch = [{"slug": x.slug, "title": x.title, "track": x.meta.get("track"), "difficulty": x.meta.get("difficulty"),
                "language": x.meta.get("language")} for x in n.values()
               if x.type == "challenge" and x.meta.get("status", "open") == "open"]
    best = max(rt.items(), key=lambda kv: (kv[1]["attempted"] > 0, kv[1]["rating"]))
    challenges = {"open": open_ch[:4], "solved": sum(v["solved"] for v in rt.values()),
                  "best_track": best[0], "best_rating": best[1]["rating"], "best_title": arena_mod.title_for(best[1]["rating"])}
    return web.json_response({"due": due, "ready": ready, "work": work[:6], "domains": domains, "activity": activity,
                              "challenges": challenges,
                              "latest_plan": plans[0].relative_to(kg.ROOT).as_posix() if plans else None})


async def start(request: web.Request) -> web.Response:
    d = await body(request)
    slugs = d.get("slugs") if isinstance(d.get("slugs"), list) else None
    return web.json_response(await tutor(request).start_assessment(
        d.get("topic", ""), d.get("slug"), d.get("questions"), [str(s) for s in slugs] if slugs else None))


async def answer(request: web.Request) -> web.Response:
    d = await body(request)
    try:
        choice = int(d.get("choice"))
    except (TypeError, ValueError):
        raise TutorError("choice must be -1 (I don't know) or 0-3")
    return web.json_response(await tutor(request).answer(
        request.match_info["id"], choice, d.get("reasoning", ""), str(d.get("confidence") or "think")))


async def report(request: web.Request) -> web.Response:
    return web.json_response(await tutor(request).make_report(request.match_info["id"]))


async def lesson(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response(await tutor(request).start_lesson(d.get("slug"), str(d.get("topic") or "")))


async def lesson_learned(request: web.Request) -> web.Response:
    return web.json_response(tutor(request).mark_learned(request.match_info["id"]))


async def chat(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response({"reply": await tutor(request).chat(request.match_info["id"], d.get("message", ""))})


async def apply(request: web.Request) -> web.Response:
    d = await body(request)
    ratings = d.get("ratings") or {}
    if not isinstance(ratings, dict):
        raise TutorError("ratings must be an object")
    return web.json_response({"applied": tutor(request).apply_ratings(request.match_info["id"], ratings)})


# ---- build page (projects + open source)

def builder(request: web.Request) -> Builder:
    return request.app["builder"]


async def build_page(request: web.Request) -> web.Response:
    return web.Response(text=kg.render_page(BUILD_TEMPLATE), content_type="text/html")


async def build_summary(request: web.Request) -> web.Response:
    await builder(request).account.ensure()
    return web.json_response(builder(request).summary())


async def github_account(request: web.Request) -> web.Response:
    await builder(request).account.ensure()
    return web.json_response(builder(request).account.public())


async def github_connect(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response(await builder(request).account.connect(str(d.get("username") or ""),
                                                                     str(d.get("token") or "")))


async def github_device_start(request: web.Request) -> web.Response:
    return web.json_response(await builder(request).account.device_start())


async def github_device_poll(request: web.Request) -> web.Response:
    return web.json_response(await builder(request).account.device_poll())


async def github_signout(request: web.Request) -> web.Response:
    return web.json_response(await builder(request).account.sign_out())


async def build_sync(request: web.Request) -> web.Response:
    return web.json_response(await builder(request).sync())


async def build_issues(request: web.Request) -> web.Response:
    d = await body(request)
    as_list = lambda v: [str(x) for x in v] if isinstance(v, list) else [s for s in str(v or "").split(",")]
    issues = await builder(request).find_issues(as_list(d.get("languages")), as_list(d.get("labels")),
                                                str(d.get("keywords") or ""), int(d.get("min_stars") or 0))
    return web.json_response({"issues": issues})


async def build_track(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response(builder(request).track_issue(str(d.get("url") or "")))


async def build_suggest(request: web.Request) -> web.Response:
    d = await body(request)
    ideas = await builder(request).suggest_projects(str(d.get("focus") or "any"), float(d.get("hours") or 15),
                                                   str(d.get("interests") or "")[:500])
    return web.json_response({"ideas": ideas})


async def build_create(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response(builder(request).create_project(str(d.get("title") or "")))


async def build_update(request: web.Request) -> web.Response:
    d = await body(request)
    milestone = d.get("milestone")
    return web.json_response(builder(request).update_project(
        request.match_info["slug"], d.get("status"), d.get("repo"),
        int(milestone) if milestone is not None else None, d.get("done")))


# ---- arena (development challenges)

def arena(request: web.Request) -> Arena:
    return request.app["arena"]


async def arena_page(request: web.Request) -> web.Response:
    return web.Response(text=kg.render_page(ARENA_TEMPLATE), content_type="text/html")


async def arena_overview(request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(arena(request).overview))


async def arena_new(request: web.Request) -> web.Response:
    d = await body(request)
    diff = d.get("difficulty")
    return web.json_response(await arena(request).generate(
        str(d.get("track") or "tooling"), str(d.get("language") or "python"), int(diff) if diff else None,
        d.get("concept") or None, str(d.get("theme") or "")[:300]))


async def arena_detail(request: web.Request) -> web.Response:
    return web.json_response(arena(request).detail(request.match_info["slug"]))


async def arena_run(request: web.Request) -> web.Response:
    return web.json_response(await arena(request).judge(request.match_info["slug"], submit=False))


async def arena_submit(request: web.Request) -> web.Response:
    return web.json_response(await arena(request).judge(request.match_info["slug"], submit=True))


async def arena_hint(request: web.Request) -> web.Response:
    return web.json_response(arena(request).hint(request.match_info["slug"]))


async def arena_giveup(request: web.Request) -> web.Response:
    return web.json_response(arena(request).give_up(request.match_info["slug"]))


async def arena_review(request: web.Request) -> web.Response:
    return web.json_response(await arena(request).review(request.match_info["slug"]))


def make_app(provider, questions: int, speech_lang: str = "en-US") -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=1 << 20)
    app["tutor"] = Tutor(provider, questions=questions)
    app["builder"] = Builder(app["tutor"], kg.load_settings().get("github") or {})
    app["arena"] = Arena(app["tutor"])
    app["speech_lang"] = speech_lang

    async def on_startup(app):
        await app["tutor"].start()

    async def on_cleanup(app):
        await app["builder"].close()
        await app["tutor"].stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.add_routes([
        web.get("/", index),
        web.get("/graph", graph_page),
        web.get("/build", build_page),
        web.get("/api/health", health),
        web.get("/api/graph", graph),
        web.get("/api/today", today),
        web.post("/api/assessments", start),
        web.post("/api/assessments/{id}/answer", answer),
        web.post("/api/assessments/{id}/report", report),
        web.post("/api/assessments/{id}/chat", chat),
        web.post("/api/assessments/{id}/apply", apply),
        web.post("/api/lessons", lesson),
        web.post("/api/lessons/{id}/learned", lesson_learned),
        web.get("/api/build", build_summary),
        web.get("/api/github/account", github_account),
        web.post("/api/github/connect", github_connect),
        web.post("/api/github/device/start", github_device_start),
        web.post("/api/github/device/poll", github_device_poll),
        web.post("/api/github/signout", github_signout),
        web.post("/api/build/sync", build_sync),
        web.post("/api/build/issues", build_issues),
        web.post("/api/build/track", build_track),
        web.post("/api/build/ideas", build_suggest),
        web.post("/api/build/projects", build_create),
        web.post("/api/build/projects/{slug}", build_update),
        web.get("/arena", arena_page),
        web.get("/api/arena", arena_overview),
        web.post("/api/arena/new", arena_new),
        web.get("/api/arena/{slug}", arena_detail),
        web.post("/api/arena/{slug}/run", arena_run),
        web.post("/api/arena/{slug}/submit", arena_submit),
        web.post("/api/arena/{slug}/hint", arena_hint),
        web.post("/api/arena/{slug}/giveup", arena_giveup),
        web.post("/api/arena/{slug}/review", arena_review),
    ])
    return app


def run(port: int | None = None, model: str | None = None, open_browser: bool = False,
        provider: str | None = None) -> int:
    cfg = (kg.load_settings().get("tutor") or {})
    port = port or int(cfg.get("port", DEFAULT_PORT))
    try:
        backend = make_provider(cfg, provider=provider, model=model)
    except TutorError as exc:
        print(f"kg serve: {exc}", file=sys.stderr)
        return 2
    questions = int(cfg.get("questions", 6))
    url = f"http://127.0.0.1:{port}/"
    print(f"Knowledge graph + tutor on {url}  ·  build {url}build  ·  challenges {url}arena  "
          f"({backend.label}, model: {backend.model}; Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    web.run_app(make_app(backend, questions, cfg.get("speech_lang", "en-US")), host="127.0.0.1", port=port, print=None)
    return 0
