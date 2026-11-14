"""Pluggable AI backends for the tutor, builder and arena.

Providers, chosen with `tutor.provider` in kg.config.yaml (or `kg serve --provider`):

- claude-code  Claude via the Claude Code CLI, using your Claude subscription login (no API key). The Claude
               desktop app ships this CLI; sign in once with `claude auth login`.
- copilot      GitHub Copilot SDK (uses your Copilot subscription; needs `gh auth login`)
- anthropic    Claude via the Anthropic API (needs ANTHROPIC_API_KEY)
- openai       any OpenAI-compatible endpoint: OpenAI, Google Gemini (OpenAI-compatible URL), Ollama,
               LM Studio, etc. (set `base_url` for anything other than api.openai.com)

Every provider exposes the same thing: a chat session that keeps its own history, with optional tools, and a
`send(prompt, timeout)` that returns text. SDKs are imported lazily, so you only need the one you use.
"""
from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import kg

PROVIDERS = ("claude-code", "copilot", "anthropic", "openai")
COPILOT_DEFAULT_MODEL = "claude-sonnet-5.5"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5-5"
CLAUDE_CODE_DEFAULT_MODEL = "sonnet"
MAX_TOOL_ROUNDS = 12
COPILOT_START_TIMEOUT = 60


class TutorError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class ToolDef:
    """A tool the model may call. `handler` gets the parsed arguments and returns text for the model."""
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Awaitable[str]]


class ChatSession(abc.ABC):
    """A conversation that keeps its own history."""

    @abc.abstractmethod
    async def _send(self, prompt: str, timeout: float) -> str: ...

    async def send(self, prompt: str, timeout: float = 240) -> str:
        return await asyncio.wait_for(self._send(prompt, timeout), timeout)

    async def close(self) -> None:
        return None


class Provider(abc.ABC):
    label: str = "AI"

    def __init__(self, model: str):
        self.model = model

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @abc.abstractmethod
    async def open_session(self, system: str, tools: list[ToolDef]) -> ChatSession: ...


async def _run_tool(tool: ToolDef | None, name: str, args: dict) -> str:
    if tool is None:
        return f"error: unknown tool {name}"
    try:
        return await tool.handler(args)
    except Exception as exc:  # the model sees the failure and can recover
        return f"error: {exc}"


# --------------------------------------------------------------------------- claude code (subscription)

NO_TOOLS_NOTE = ("\n\nNote: the search_books and get_concept tools are NOT available in this session. Do not try to "
                 "call them. Teach and assess from your own knowledge and the context given in the messages, and "
                 "leave every `source`/`sources` field empty (never invent book citations).")


def _find_claude() -> str | None:
    """The `claude` CLI: on PATH, the standalone install, or the copy bundled with the Claude desktop app."""
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for p in (home / ".claude" / "local" / "claude", home / ".local" / "bin" / "claude"):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    bundled = list(home.glob("Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"))

    def version(p: Path) -> tuple[int, ...]:
        m = re.search(r"claude-code/(\d+(?:\.\d+)*)/", p.as_posix())
        return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)

    return str(max(bundled, key=version)) if bundled else None


class ClaudeCodeProvider(Provider):
    label = "Claude (subscription)"

    def __init__(self, model: str | None, claude_path: str | None = None):
        super().__init__(model or "default")
        self.cli_model = model
        self.path = claude_path or _find_claude()
        if not self.path:
            raise TutorError("couldn't find the Claude Code CLI. Install Claude Code (or the Claude desktop app), "
                             "or set tutor.claude_path in kg.config.yaml", 500)
        self.workdir = kg.ROOT / ".kg" / "claude-code"  # empty folder: no project CLAUDE.md or settings leak in

    async def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)

    async def open_session(self, system: str, tools: list[ToolDef]) -> ChatSession:
        return ClaudeCodeChat(self, system + (NO_TOOLS_NOTE if tools else ""))


class ClaudeCodeChat(ChatSession):
    """Each turn runs `claude -p`; later turns `--resume` the same CLI session so the history carries over."""

    def __init__(self, provider: ClaudeCodeProvider, system: str):
        self.p = provider
        self.system = system
        self.session_id: str | None = None

    async def _send(self, prompt: str, timeout: float) -> str:
        args = [self.p.path, "-p", "--output-format", "json", "--tools", "", "--setting-sources", "",
                "--system-prompt", self.system]
        if self.p.cli_model:
            args += ["--model", self.p.cli_model]
        if self.session_id:
            args += ["--resume", self.session_id]
        # A server started from inside a Claude Code session inherits markers that change how the CLI behaves.
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self.p.workdir), env=env)
        try:
            out, err = await proc.communicate(prompt.encode("utf-8"))
        except BaseException:  # timeout or shutdown: don't leave the CLI running
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise
        try:
            data = json.loads(out.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            detail = (err or out).decode("utf-8", "replace").strip()[:300]
            raise TutorError(f"Claude Code failed (exit {proc.returncode}): {detail}", 502)
        if data.get("is_error"):
            msg = str(data.get("result") or data.get("subtype") or "unknown error")
            if "log in" in msg.lower() or "login" in msg.lower():
                raise TutorError(f"Claude Code isn't signed in. In a terminal run:  \"{self.p.path}\" auth login  "
                                 "(sign in with your Claude account), then try again.", 401)
            raise TutorError(f"Claude Code: {msg[:300]}", 502)
        self.session_id = data.get("session_id") or self.session_id
        return str(data.get("result") or "")


# --------------------------------------------------------------------------- copilot

def _github_token() -> str | None:
    """Token for Copilot: env var first, otherwise the `gh` CLI login."""
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    gh = shutil.which("gh")
    if not gh:  # the Copilot desktop app bundles its own gh.exe that isn't on PATH
        local = os.environ.get("LOCALAPPDATA", "")
        found = sorted(Path(local).glob("copilot-desktop-gh-*/gh.exe")) if local else []
        gh = str(found[-1]) if found else None
    if gh:
        try:
            out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15)
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


class CopilotProvider(Provider):
    label = "GitHub Copilot"

    def __init__(self, model: str):
        super().__init__(model)
        self.client = None

    async def start(self) -> None:
        from copilot import CopilotClient
        (kg.ROOT / ".kg" / "copilot").mkdir(parents=True, exist_ok=True)
        kwargs = {}
        token = _github_token()
        if token:  # the server may be launched from a terminal that has no GH_TOKEN in its environment
            kwargs["github_token"] = token
        self.client = CopilotClient(mode="empty", base_directory=str(kg.ROOT / ".kg" / "copilot"), **kwargs)
        try:
            await asyncio.wait_for(self.client.start(), COPILOT_START_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(
                "GitHub Copilot didn't start within a minute. Sign in with `gh auth login` (an account with Copilot)"
                + ("" if token else " — no GitHub token was found") + ", or pick another tutor.provider.") from None

    async def stop(self) -> None:
        if self.client:
            with contextlib.suppress(Exception):
                await self.client.stop()

    async def open_session(self, system: str, tools: list[ToolDef]) -> ChatSession:
        from copilot import ToolSet
        from copilot.session import PermissionHandler
        from copilot.tools import Tool, ToolResult

        def wrap(t: ToolDef):
            async def handler(inv):
                text = await _run_tool(t, t.name, inv.arguments or {})
                failed = text.startswith("error:")
                return ToolResult(text_result_for_llm=text, **({"result_type": "failure"} if failed else {}))
            return Tool(name=t.name, skip_permission=True, handler=handler, description=t.description,
                        parameters=t.parameters)

        kwargs = dict(on_permission_request=PermissionHandler.approve_all, model=self.model,
                      system_message={"mode": "replace", "content": system})
        if tools:
            names = ToolSet()
            for t in tools:
                names = names.add_custom(t.name)
            kwargs["tools"] = [wrap(t) for t in tools]
            kwargs["available_tools"] = names.to_list()
        else:
            kwargs["available_tools"] = []
        return CopilotChat(await self.client.create_session(**kwargs))


class CopilotChat(ChatSession):
    def __init__(self, session):
        self.session = session

    async def _send(self, prompt: str, timeout: float) -> str:
        event = await self.session.send_and_wait(prompt, timeout=timeout)
        return (event.data.content if event else "") or ""

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.session.disconnect()


# --------------------------------------------------------------------------- anthropic API

class AnthropicProvider(Provider):
    label = "Claude API"

    def __init__(self, model: str, api_key_env: str = "ANTHROPIC_API_KEY", max_tokens: int = 16000):
        super().__init__(model)
        key = os.environ.get(api_key_env)
        if not key:
            raise TutorError(f"the anthropic provider needs an API key: set {api_key_env}", 500)
        try:
            import anthropic
        except ImportError:
            raise TutorError("the anthropic provider needs the SDK: pip install anthropic", 500)
        self.client = anthropic.AsyncAnthropic(api_key=key)
        self.max_tokens = max_tokens  # lessons and reports are long JSON objects; too low truncates them

    async def open_session(self, system: str, tools: list[ToolDef]) -> ChatSession:
        return AnthropicChat(self, system, tools)


class AnthropicChat(ChatSession):
    def __init__(self, provider: AnthropicProvider, system: str, tools: list[ToolDef]):
        self.p = provider
        self.system = system
        self.tools = {t.name: t for t in tools}
        self.specs = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        self.messages: list[dict] = []

    async def _send(self, prompt: str, timeout: float) -> str:
        mark = len(self.messages)
        try:
            return await self._turn(prompt)
        except BaseException:
            # a half-finished turn (e.g. tool_use without its tool_result) would break every later request
            del self.messages[mark:]
            raise

    async def _turn(self, prompt: str) -> str:
        self.messages.append({"role": "user", "content": prompt})
        for _ in range(MAX_TOOL_ROUNDS):
            kwargs = dict(model=self.p.model, max_tokens=self.p.max_tokens, system=self.system, messages=self.messages)
            if self.specs:
                kwargs["tools"] = self.specs
            resp = await self.p.client.messages.create(**kwargs)
            self.messages.append({"role": "assistant",
                                  "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                return "".join(b.text for b in resp.content if b.type == "text")
            results = [{"type": "tool_result", "tool_use_id": u.id,
                        "content": await _run_tool(self.tools.get(u.name), u.name, u.input or {})} for u in uses]
            self.messages.append({"role": "user", "content": results})
        raise TutorError("the model kept calling tools without answering; please retry", 502)


# --------------------------------------------------------------------------- openai-compatible

class OpenAIProvider(Provider):
    label = "OpenAI-compatible API"

    def __init__(self, model: str, base_url: str | None = None, api_key_env: str = "OPENAI_API_KEY"):
        super().__init__(model)
        key = os.environ.get(api_key_env)
        if not key and base_url:  # local servers such as Ollama ignore the key but the SDK requires one
            key = "not-needed"
        if not key:
            raise TutorError(f"the openai provider needs an API key: set {api_key_env}", 500)
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise TutorError("the openai provider needs the SDK: pip install openai", 500)
        self.client = AsyncOpenAI(api_key=key, base_url=base_url or None)

    async def open_session(self, system: str, tools: list[ToolDef]) -> ChatSession:
        return OpenAIChat(self, system, tools)


class OpenAIChat(ChatSession):
    def __init__(self, provider: OpenAIProvider, system: str, tools: list[ToolDef]):
        self.p = provider
        self.tools = {t.name: t for t in tools}
        self.specs = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                         "parameters": t.parameters}} for t in tools]
        self.messages: list[dict] = [{"role": "system", "content": system}]

    async def _send(self, prompt: str, timeout: float) -> str:
        mark = len(self.messages)
        try:
            return await self._turn(prompt)
        except BaseException:
            del self.messages[mark:]
            raise

    async def _turn(self, prompt: str) -> str:
        self.messages.append({"role": "user", "content": prompt})
        for _ in range(MAX_TOOL_ROUNDS):
            kwargs = dict(model=self.p.model, messages=self.messages)
            if self.specs:
                kwargs["tools"] = self.specs
            resp = await self.p.client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            # echo back only the standard fields: some compatible servers reject OpenAI-only extras
            turn: dict = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                turn["tool_calls"] = [{"id": c.id, "type": "function",
                                       "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                      for c in msg.tool_calls]
            self.messages.append(turn)
            if not msg.tool_calls:
                return msg.content or ""
            for call in msg.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    out = f"error: arguments were not valid JSON ({exc})"
                else:
                    out = await _run_tool(self.tools.get(call.function.name), call.function.name, args)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": out})
        raise TutorError("the model kept calling tools without answering; please retry", 502)


# --------------------------------------------------------------------------- factory

def make_provider(cfg: dict, provider: str | None = None, model: str | None = None) -> Provider:
    """Build the provider named in config (`tutor.provider`), with optional CLI overrides."""
    configured = str(cfg.get("provider") or "copilot").strip().lower()
    name = str(provider or configured).strip().lower()
    if name not in PROVIDERS:
        raise TutorError(f"unknown provider '{name}'; choose one of: {', '.join(PROVIDERS)}", 500)
    if not model and name == configured:  # tutor.model belongs to the configured provider, not to an override
        model = cfg.get("model") or None
    if name == "claude-code":
        return ClaudeCodeProvider(model or CLAUDE_CODE_DEFAULT_MODEL, claude_path=cfg.get("claude_path") or None)
    if name == "copilot":
        return CopilotProvider(model or COPILOT_DEFAULT_MODEL)
    if name == "anthropic":
        return AnthropicProvider(model or ANTHROPIC_DEFAULT_MODEL,
                                 api_key_env=cfg.get("api_key_env") or "ANTHROPIC_API_KEY",
                                 max_tokens=int(cfg.get("max_tokens") or 16000))
    if not model:
        raise TutorError("set tutor.model in kg.config.yaml for the openai provider (e.g. gpt-5 or llama3.1)", 500)
    return OpenAIProvider(model, base_url=cfg.get("base_url"), api_key_env=cfg.get("api_key_env") or "OPENAI_API_KEY")
