import asyncio
import json
import os
import stat
import sys

import pytest

import builder as B
import kg
import llm
from conftest import note
from tutor import TutorError


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "ACCOUNT_FILE", tmp_path / "github_account.json")
    monkeypatch.setattr(B, "SYNC_STATE", tmp_path / "github_sync.json")
    monkeypatch.setattr(B, "ambient_token", lambda: (None, None))
    return tmp_path


def test_concurrent_ensure_sees_the_saved_account(state, monkeypatch):  # B7
    B.ACCOUNT_FILE.write_text(json.dumps({"login": "alice", "method": "username"}))

    async def slow_set(self, token):
        await asyncio.sleep(0.05)
        self.token = token
    monkeypatch.setattr(B.GitHub, "set_token", slow_set)

    async def go():
        b = B.Builder(None, {})
        await asyncio.gather(b.account.ensure(), b.account.ensure())
        return b.username
    assert asyncio.run(go()) == "alice"


def test_token_file_is_private_or_in_keyring(state, monkeypatch):  # S9
    acc = B.Account(B.GitHub(), None)
    acc._write({"login": "bob", "token": "tok"})
    if os.name != "nt":   # Windows has no Unix mode bits; there the profile folder's ACL protects the file
        assert stat.S_IMODE(os.stat(B.ACCOUNT_FILE).st_mode) == 0o600
    assert acc._read()["token"] == "tok"

    store = {}

    class FakeKeyring:
        def set_password(self, s, u, p): store[(s, u)] = p
        def get_password(self, s, u): return store.get((s, u))
        def delete_password(self, s, u): store.pop((s, u), None)
    monkeypatch.setattr(B, "_keyring", lambda: FakeKeyring())
    acc._write({"login": "bob", "token": "tok2"})
    assert "tok2" not in B.ACCOUNT_FILE.read_text() and acc._read()["token"] == "tok2"
    acc._write({"signed_out": True})
    assert not store


def test_update_fields_writes_only_on_change(tmp_path):  # B9
    p = note(tmp_path / "x.md", "title: X\nstars: 3")
    before = p.stat().st_mtime_ns
    assert B.update_fields(kg.parse_note(p), {"stars": 3}) is False
    assert p.stat().st_mtime_ns == before
    assert B.update_fields(kg.parse_note(p), {"stars": 4}) is True


def test_sync_survives_a_failed_tagging_batch_and_keeps_similar_repos_apart(state, vault, monkeypatch):  # B9, B6
    repos = [{"name": n, "full_name": f"me/{n}", "html_url": f"https://github.com/me/{n}", "fork": False,
              "pushed_at": "2026-11-01T00:00:00Z", "stargazers_count": 1, "language": "Python", "description": ""}
             for n in ("foo_bar", "foo-bar")]

    async def get(self, path, params=None, raw=False):
        if path.endswith("/readme"):
            return ""
        if path.startswith("/users/me/repos"):
            return repos if (params or {}).get("page", 1) == 1 else []
        raise AssertionError(path)

    async def search(self, q, per_page=100, sort=None, pages=10):
        return []

    class FailingTutor:
        async def ask_typed(self, *a, **k):
            raise TutorError("model down", 502)
    monkeypatch.setattr(B.GitHub, "get", get)
    monkeypatch.setattr(B.GitHub, "search_issues", search)
    b = B.Builder(FailingTutor(), {})
    b.account.info, b.account.checked = {"login": "me"}, True
    res = asyncio.run(b.sync())
    assert res["new"] == 2 and res["untagged"] == 2
    files = sorted(p.name for p in (vault / "projects").glob("*.md"))
    assert len(files) == 2 and files[0] != files[1]


def test_claude_code_strips_api_keys_and_passes_prompt_by_file(tmp_path, monkeypatch):  # S12, B8
    script = tmp_path / "fake_claude.py"
    log = tmp_path / "log.json"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
spf = args[args.index("--system-prompt-file") + 1]
json.dump({{"env": [k for k in os.environ if k.startswith("ANTHROPIC")], "system": open(spf).read(),
           "stdin": sys.stdin.read(), "args": args}}, open({str(log)!r}, "w"))
print(json.dumps({{"result": "hi", "session_id": "s1"}}))
""")
    if os.name == "nt":   # Windows can't run a #! script: give it a .cmd launcher, like npm's claude.cmd shim
        fake = tmp_path / "claude.cmd"
        fake.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        fake = script
        fake.chmod(0o755)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "x")
    monkeypatch.setattr(llm.kg, "ROOT", tmp_path)
    p = llm.ClaudeCodeProvider("sonnet", claude_path=str(fake))

    async def go():
        await p.start()
        s = await p.open_session("system <with> 'quotes'", [])
        return await s.send("question", 30)
    assert asyncio.run(go()) == "hi"
    got = json.loads(log.read_text())
    assert got["env"] == [] and got["system"] == "system <with> 'quotes'" and got["stdin"] == "question"
    assert "--system-prompt" not in got["args"]
    assert not list((tmp_path / ".kg" / "claude-code").glob("system-*.txt"))   # temp prompt file cleaned up


def test_default_provider_matches_readme(monkeypatch):  # B8
    monkeypatch.setattr(llm, "_find_claude", lambda: "/bin/echo")
    assert isinstance(llm.make_provider({}), llm.ClaudeCodeProvider)


def test_copilot_permission_requests_are_denied():  # S13
    pytest.importorskip("copilot")
    from copilot.generated.rpc import PermissionDecisionReject
    assert isinstance(llm._deny_permission(object(), {}), PermissionDecisionReject)


def test_anthropic_cut_off_reply_is_reported(monkeypatch):  # B10
    class Resp:
        stop_reason = "max_tokens"
        content = []

    class Client:
        class messages:
            @staticmethod
            async def create(**kw):
                return Resp()
    p = llm.AnthropicProvider.__new__(llm.AnthropicProvider)
    p.model, p.max_tokens, p.client = "m", 10, Client()
    chat = llm.AnthropicChat(p, "sys", [])
    with pytest.raises(TutorError, match="cut off"):
        asyncio.run(chat.send("hi", 5))
    assert chat.messages == []   # rolled back
