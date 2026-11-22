"""Request guard: Host allow-list (S2), per-launch key cookie (S18), security headers (S15), vendored libs (S11)."""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

import server
from llm import Provider

TOKEN = "t" * 43


class FakeProvider(Provider):
    label = "fake"

    async def open_session(self, system, tools):
        raise AssertionError("no AI calls in these tests")


def run(coro):
    return asyncio.run(coro)


async def client(port=8765):
    app = server.make_app(FakeProvider("m"), 6, port=port, token=TOKEN)
    srv = TestServer(app, host="127.0.0.1", port=port + 1000)
    c = TestClient(srv)
    await c.start_server()
    return c


def host_hdr(c, host=None):
    return {"Host": host or "127.0.0.1:8765"}


def test_rebinding_host_is_rejected_for_get_and_post():
    async def go():
        c = await client()
        try:
            r = await c.get("/api/graph", headers=host_hdr(c, "evil.example:8765"),
                            cookies={"kg8765": TOKEN})
            assert r.status == 403
            r = await c.post("/api/profile", json={"focus": ["math"]},
                             headers={**host_hdr(c, "evil.example:8765"), "X-KG": "1",
                                      "Origin": "http://evil.example:8765"})
            assert r.status == 403
        finally:
            await c.close()
    run(go())


def test_key_is_required_and_becomes_a_strict_cookie():
    async def go():
        c = await client()
        try:
            r = await c.get("/api/graph", headers=host_hdr(c))
            assert r.status == 403                                 # no key
            r = await c.get("/", headers=host_hdr(c))
            assert r.status == 403 and "own link" in await r.text()
            r = await c.get("/today?kgtoken=" + "x" * 43, headers=host_hdr(c), allow_redirects=False)
            assert r.status == 403                                 # wrong key
            r = await c.get("/today?a=1&kgtoken=" + TOKEN, headers=host_hdr(c), allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/today?a=1"
            cookie = r.headers["Set-Cookie"]
            assert "kg8765=" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        finally:
            await c.close()
    run(go())


def test_health_without_key_reveals_nothing_personal():
    async def go():
        c = await client()
        try:
            r = await c.get("/api/health", headers=host_hdr(c))
            assert r.status == 200 and set(await r.json()) == {"ok", "root"}
        finally:
            await c.close()
    run(go())


def test_security_headers_and_cross_origin_post():
    async def go():
        c = await client()
        try:
            r = await c.get("/api/profile", headers=host_hdr(c), cookies={"kg8765": TOKEN})
            assert r.status == 200
            csp = r.headers["Content-Security-Policy"]
            assert "frame-ancestors 'self'" in csp and "connect-src 'self'" in csp and "unpkg" not in csp
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["Referrer-Policy"] == "no-referrer"
            r = await c.post("/api/profile", json={}, headers={**host_hdr(c), "X-KG": "1",
                                                              "Origin": "http://127.0.0.1:9999"},
                             cookies={"kg8765": TOKEN})
            assert r.status == 403                                 # other origin
            r = await c.post("/api/profile", json={}, headers=host_hdr(c), cookies={"kg8765": TOKEN})
            assert r.status == 403                                 # no X-KG header
        finally:
            await c.close()
    run(go())


def test_vendored_scripts_are_served_and_templates_use_them():
    async def go():
        c = await client()
        try:
            r = await c.get("/vendor/purify.min.js", headers=host_hdr(c), cookies={"kg8765": TOKEN})
            assert r.status == 200 and "javascript" in r.headers["Content-Type"]
            r = await c.get("/vendor/..%2Fserver.py", headers=host_hdr(c), cookies={"kg8765": TOKEN})
            assert r.status == 404
        finally:
            await c.close()
    run(go())
    for t in server.kg.ROOT.joinpath("tools").glob("*_template.html"):
        assert "unpkg.com" not in t.read_text(encoding="utf-8"), t.name


def test_shell_only_opens_same_origin_section_paths():  # S1
    html = (server.kg.ROOT / "tools" / "shell_template.html").read_text(encoding="utf-8")
    assert "if (u.origin !== location.origin) return;" in html
    assert "if (!tab) return;" in html
    assert "location.href = u.href" not in html


def test_second_server_refuses_a_busy_port_and_keeps_the_key_file(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(server, "TOKEN_FILE", tmp_path / "server.token")
    server.TOKEN_FILE.write_text('{"token": "running-server-key"}')
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert "port" in server._port_problem(port)
        monkeypatch.setattr(server, "make_provider", lambda *a, **k: FakeProvider("m"))
        monkeypatch.setattr(server.kg, "load_settings", lambda: {"tutor": {}})
        assert server.run(port=port) == 1
    assert server.TOKEN_FILE.read_text() == '{"token": "running-server-key"}'
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert server._port_problem(free) == ""
