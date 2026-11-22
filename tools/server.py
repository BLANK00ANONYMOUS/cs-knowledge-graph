"""Local web app: live knowledge-graph viewer + AI tutor API (bound to 127.0.0.1)."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import signal
import sys
import time
import urllib.parse
import webbrowser

from aiohttp import web

import kg
from arena import Arena
from jobs import Jobs
from topics import Topics
from builder import Builder
from llm import make_provider
from tutor import Tutor, TutorError

DEFAULT_PORT = 8765
BUILD_TEMPLATE = kg.ROOT / "tools" / "build_template.html"
TODAY_TEMPLATE = kg.ROOT / "tools" / "today_template.html"
ARENA_TEMPLATE = kg.ROOT / "tools" / "arena_template.html"
JOBS_TEMPLATE = kg.ROOT / "tools" / "jobs_template.html"
SHELL_TEMPLATE = kg.ROOT / "tools" / "shell_template.html"
VENDOR_DIR = kg.ROOT / "tools" / "vendor"
TOKEN_FILE = kg.ROOT / ".kg" / "server.token"
# identifies this checkout, so a launcher never mistakes another program (or another clone's server) for this one
ROOT_ID = hashlib.sha1(str(kg.ROOT).encode()).hexdigest()[:12]

# Templates use inline scripts/styles, so those need 'unsafe-inline'; everything else is locked to this origin.
# img-src/connect-src stop model or posting text from leaking data through image URLs; frame-ancestors stops
# other sites from framing the app (clickjacking).
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https://avatars.githubusercontent.com; font-src 'self' data:; connect-src 'self'; "
       "frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; object-src 'none'; form-action 'self'; "
       "media-src 'self'; worker-src 'self' blob:")
FORBIDDEN_PAGE = ("<!doctype html><meta charset=utf-8><title>Knowledge Graph</title><body style='font:16px system-ui;"
                  "max-width:40rem;margin:4rem auto;line-height:1.5'><h1>Open the app from its own link</h1><p>This "
                  "server only answers the window it opened. Start it with <code>kg serve --open</code> or the Knowledge "
                  "Graph shortcut, or use the link that <code>kg serve</code> printed (it contains a one-time key).</p>")


def notes():
    vault, _ = kg.load_config()
    return kg.load_vault_cached(vault)[0]


def _authed(request: web.Request) -> bool:
    token = request.app["token"]
    return bool(token) and hmac.compare_digest(request.cookies.get(request.app["cookie"], ""), token)


def _forbidden(request: web.Request) -> web.Response:
    if request.path.startswith("/api/") or request.method != "GET":
        return web.json_response({"error": "forbidden"}, status=403)
    return web.Response(text=FORBIDDEN_PAGE, content_type="text/html", status=403)


@web.middleware
async def guard(request: web.Request, handler):
    app = request.app
    # DNS rebinding: a hostile site's name resolving to 127.0.0.1 still sends its own Host header.
    if request.host not in app["hosts"]:
        return web.json_response({"error": "forbidden host"}, status=403)
    if request.method == "POST":
        # cross-site POSTs: our custom header forces a CORS preflight we never answer, and Origin must be us
        origin = request.headers.get("Origin")
        if request.headers.get("X-KG") != "1" or (origin and origin not in app["origins"]):
            return web.json_response({"error": "forbidden"}, status=403)
    # Per-launch key: the window the server opened gets it once in the URL and keeps it as a SameSite=Strict,
    # HttpOnly cookie. Links from other sites arrive without that cookie, other local users don't have the key.
    request["authed"] = _authed(request)
    if not request["authed"]:
        given = request.query.get("kgtoken", "")
        if app["token"] and given and hmac.compare_digest(given, app["token"]):
            query = [(k, v) for k, v in request.query.items() if k != "kgtoken"]
            target = request.path + ("?" + urllib.parse.urlencode(query) if query else "")
            resp = web.HTTPFound(target if target.startswith("/") and not target.startswith("//") else "/")
            resp.set_cookie(app["cookie"], app["token"], httponly=True, samesite="Strict", path="/")
            raise resp
        if request.path != "/api/health" and app["token"]:
            return _forbidden(request)
    app["activity"]["last"] = time.monotonic()
    try:
        resp = await handler(request)
        _harden(resp)
        return resp
    except TutorError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)
    except asyncio.TimeoutError:
        return web.json_response({"error": "the model took too long; try again"}, status=504)
    except web.HTTPException as exc:
        _harden(exc)
        raise
    except Exception as exc:  # surface model/SDK errors to the UI instead of a bare 500
        msg = f"{type(exc).__name__}: {exc}"
        if "OAuth token" in msg or "HMAC" in msg:
            msg = ("Not signed in to GitHub Copilot. In a terminal run `gh auth login` (use your GitHub account that "
                   "has Copilot), then restart `.\\kg.cmd serve`.")
        return web.json_response({"error": msg}, status=500)


def _harden(resp) -> None:
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")


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
    """The app shell: every section stays loaded in its own frame, so switching tabs never loses work."""
    return web.Response(text=kg.render_page(SHELL_TEMPLATE), content_type="text/html")


async def today_page(request: web.Request) -> web.Response:
    return web.Response(text=kg.render_page(TODAY_TEMPLATE), content_type="text/html")


async def graph_page(request: web.Request) -> web.Response:
    html = await asyncio.to_thread(lambda: kg.render_graph_html(kg.graph_data(notes())))
    return web.Response(text=html, content_type="text/html")


async def vendor(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    path = VENDOR_DIR / name
    if "/" in name or "\\" in name or not name.endswith(".js") or not path.is_file():
        raise web.HTTPNotFound()
    resp = web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400",
                                           "Content-Type": "text/javascript; charset=utf-8"})
    return resp


async def health(request: web.Request) -> web.Response:
    # without the key: just enough for the launcher to recognise this server, nothing about the learner
    if not request.get("authed"):
        return web.json_response({"ok": True, "root": ROOT_ID})
    n = await asyncio.to_thread(notes)
    return web.json_response({"ok": True, "root": ROOT_ID, "model": tutor(request).model,
                              "provider": tutor(request).provider.label,
                              "books": sum(1 for x in n.values() if x.type == "book"),
                              "questions": tutor(request).default_questions,
                              "speech_lang": request.app["speech_lang"]})


async def graph(request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(lambda: kg.graph_data(notes())))


async def today(request: web.Request) -> web.Response:
    """Next-best-actions for the home view: reviews due, concepts ready, work in progress, recent activity."""
    await builder(request).account.ensure()
    return web.json_response(await asyncio.to_thread(_today, builder(request)))


def _today(b: Builder) -> dict:
    n = notes()
    title = lambda s: n[s].title if s in n else s
    due = [{"slug": x.slug, "title": x.title, "domain": x.domain, "mastery": x.mastery, "overdue": d}
           for d, x in kg.due_reviews(n)[:8]]
    profile = kg.load_profile()
    focus = [d for d in profile.get("focus") or [] if d in kg.DOMAINS]
    ready_all = sorted(kg.ready_concepts(n, None), key=lambda x: x.domain not in focus) if focus else kg.ready_concepts(n, None)
    ready = [{"slug": x.slug, "title": x.title, "domain": x.domain, "mastery": x.mastery, "summary": x.summary,
              "prereqs": [title(p) for p in x.refs("prereqs")]} for x in ready_all[:6]]
    work = []
    summary = b.summary()
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
    steps = profile.get("steps") or {}
    concepts_ = [x for x in n.values() if x.type == "concept"]
    onboarding = {
        "hidden": bool(profile.get("guide_hidden")), "focus": focus,
        "done": {"focus": bool(focus),
                 "quiz": "quiz" in steps or bool(plans) or any(x.mastery > 0 for x in concepts_),
                 "lesson": "lesson" in steps,
                 "topic": "topic" in steps,
                 "role": any(x.type == "role" for x in n.values()),
                 "challenge": "challenge" in steps or any(v["attempted"] for v in rt.values())},
    }
    return {"due": due, "ready": ready, "work": work[:6], "domains": domains, "activity": activity,
            "challenges": challenges, "onboarding": onboarding,
            "latest_plan": plans[0].relative_to(kg.ROOT).as_posix() if plans else None}


async def profile_get(request: web.Request) -> web.Response:
    return web.json_response(kg.load_profile())


async def profile_set(request: web.Request) -> web.Response:
    d = await body(request)
    changes = {}
    if "focus" in d:
        if not isinstance(d["focus"], list):
            raise TutorError("focus must be a list")
        changes["focus"] = [x for x in dict.fromkeys(map(str, d["focus"])) if x in kg.DOMAINS]
    if "guide_hidden" in d:
        changes["guide_hidden"] = bool(d["guide_hidden"])
    return web.json_response(kg.save_profile(changes))


# ---- topics (grow the graph on demand)

def topics(request: web.Request) -> Topics:
    return request.app["topics"]


async def topics_propose(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response(await topics(request).propose(str(d.get("text") or "")))


async def topics_create(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response(await asyncio.to_thread(topics(request).create, d.get("topics")))


async def topics_ideas(request: web.Request) -> web.Response:
    return web.json_response({"ideas": await topics(request).ideas()})


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
    res = await tutor(request).answer(
        request.match_info["id"], choice, d.get("reasoning", ""), str(d.get("confidence") or "think"))
    kg.mark_step("quiz")
    return web.json_response(res)


async def report(request: web.Request) -> web.Response:
    return web.json_response(await tutor(request).make_report(request.match_info["id"]))


async def lesson(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response(await tutor(request).start_lesson(d.get("slug"), str(d.get("topic") or "")))


async def lesson_learned(request: web.Request) -> web.Response:
    res = tutor(request).mark_learned(request.match_info["id"])
    kg.mark_step("lesson")
    return web.json_response(res)


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
    return web.json_response(await asyncio.to_thread(builder(request).summary))


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
    kg.mark_step("challenge")
    return web.json_response(await arena(request).judge(request.match_info["slug"], submit=False))


async def arena_submit(request: web.Request) -> web.Response:
    kg.mark_step("challenge")
    return web.json_response(await arena(request).judge(request.match_info["slug"], submit=True))


async def arena_hint(request: web.Request) -> web.Response:
    return web.json_response(arena(request).hint(request.match_info["slug"]))


async def arena_giveup(request: web.Request) -> web.Response:
    return web.json_response(await arena(request).give_up(request.match_info["slug"]))


async def arena_review(request: web.Request) -> web.Response:
    return web.json_response(await arena(request).review(request.match_info["slug"]))


# ---- jobs (target roles, skill gaps, job board)

def jobs(request: web.Request) -> Jobs:
    return request.app["jobs"]


async def jobs_page(request: web.Request) -> web.Response:
    return web.Response(text=kg.render_page(JOBS_TEMPLATE), content_type="text/html")


async def jobs_overview(request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(jobs(request).overview))


async def jobs_suggest(request: web.Request) -> web.Response:
    return web.json_response({"roles": await jobs(request).suggest()})


async def jobs_analyze(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response(await jobs(request).analyze(
        str(d.get("title") or "")[:120], str(d.get("jd") or "")[:12000], str(d.get("company") or "")[:80],
        str(d.get("url") or "")[:500], "jd"))


async def jobs_board(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response(await jobs(request).board(str(d.get("query") or "")[:80], bool(d.get("remote"))))


async def jobs_board_analyze(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response(await jobs(request).analyze_board(str(d.get("id") or "")))


async def jobs_detail(request: web.Request) -> web.Response:
    return web.json_response(jobs(request).detail(request.match_info["slug"]))


async def jobs_add_skills(request: web.Request) -> web.Response:
    d = await body(request)
    names = d.get("names")
    if names is not None and not isinstance(names, list):
        raise TutorError("names must be a list")
    return web.json_response(await asyncio.to_thread(jobs(request).add_skills, request.match_info["slug"],
                                                     [str(x) for x in names] if names is not None else None))


async def jobs_status(request: web.Request) -> web.Response:
    d = await body(request)
    return web.json_response(jobs(request).set_status(request.match_info["slug"], str(d.get("status") or "")))


def make_app(provider, questions: int, speech_lang: str = "en-US", port: int = DEFAULT_PORT,
             token: str | None = None, verify_answers: bool = False) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=1 << 20)
    app["hosts"] = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    app["origins"] = {f"http://{h}" for h in app["hosts"]}
    app["token"] = token if token is not None else secrets.token_urlsafe(32)
    app["cookie"] = f"kg{port}"   # cookies ignore ports: two servers on different ports must not share one
    app["activity"] = {"last": time.monotonic()}   # mutable: updated per request
    app["tutor"] = Tutor(provider, questions=questions, verify_answers=verify_answers)
    app["builder"] = Builder(app["tutor"], kg.load_settings().get("github") or {})
    app["arena"] = Arena(app["tutor"])
    app["jobs"] = Jobs(app["tutor"], (kg.load_settings().get("github") or {}))
    app["topics"] = Topics(app["tutor"])
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
        web.get("/vendor/{name}", vendor),
        web.get("/today", today_page),
        web.get("/api/profile", profile_get),
        web.post("/api/profile", profile_set),
        web.post("/api/topics/propose", topics_propose),
        web.post("/api/topics/create", topics_create),
        web.post("/api/topics/ideas", topics_ideas),
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
        web.get("/jobs", jobs_page),
        web.get("/api/jobs", jobs_overview),
        web.post("/api/jobs/suggest", jobs_suggest),
        web.post("/api/jobs/analyze", jobs_analyze),
        web.post("/api/jobs/board", jobs_board),
        web.post("/api/jobs/board/analyze", jobs_board_analyze),
        web.get("/api/jobs/{slug}", jobs_detail),
        web.post("/api/jobs/{slug}/add-skills", jobs_add_skills),
        web.post("/api/jobs/{slug}/status", jobs_status),
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


def _write_token(token: str, port: int) -> None:
    """Hand the per-launch key to the launcher (same user only: mode 600) and remember which server owns it."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"token": token, "port": port, "pid": os.getpid()}, fh)


async def _idle_watch(app: web.Application, minutes: float) -> None:
    """Stop a background server nobody has used for a while (the launcher starts it again on demand)."""
    while True:
        await asyncio.sleep(60)
        busy = any(a.lock.locked() for a in app["tutor"].assessments.values())
        if not busy and time.monotonic() - app["activity"]["last"] > minutes * 60:
            print(f"idle for {minutes:g} min; stopping", flush=True)
            signal.raise_signal(signal.SIGINT)   # run_app turns this into a clean shutdown
            return


def run(port: int | None = None, model: str | None = None, open_browser: bool = False,
        provider: str | None = None, idle_exit: float | None = None) -> int:
    cfg = (kg.load_settings().get("tutor") or {})
    port = port or int(cfg.get("port", DEFAULT_PORT))
    try:
        backend = make_provider(cfg, provider=provider, model=model)
    except TutorError as exc:
        print(f"kg serve: {exc}", file=sys.stderr)
        return 2
    questions = int(cfg.get("questions", 6))
    token = os.environ.pop("KG_TOKEN", "") or secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{port}/"
    app = make_app(backend, questions, cfg.get("speech_lang", "en-US"), port=port, token=token,
                   verify_answers=bool(cfg.get("verify_answers")))
    idle = idle_exit if idle_exit is not None else float(cfg.get("idle_exit_minutes") or 0)

    async def on_startup(app):
        _write_token(token, port)
        if idle > 0:
            app["idle_task"] = asyncio.create_task(_idle_watch(app, idle))

    async def on_cleanup(app):
        task = app.get("idle_task")
        if task:
            task.cancel()
        try:
            if json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
                TOKEN_FILE.unlink()
        except (OSError, ValueError):
            pass

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    print(f"Knowledge graph + tutor on {url}  ({backend.label}, model: {backend.model}; Ctrl+C to stop)")
    if sys.stdout.isatty():   # never write the key into a log file (other users may read it); the launcher
        print(f"Open the app with this link (it carries this launch's key): {url}?kgtoken={token}", flush=True)
    else:                     # reads it from the owner-only key file instead
        print(f"Launch key: {TOKEN_FILE} (owner-only); open the app with the shortcut or kg serve --open", flush=True)
    if open_browser:
        webbrowser.open(f"{url}?kgtoken={token}")
    web.run_app(app, host="127.0.0.1", port=port, print=None)
    return 0
