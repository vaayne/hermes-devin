"""OpenAI-client-compatible facade over the Devin/Cascade Connect API.

Port of devin-gateway's convert.ts + client.ts: OpenAI messages become Cascade
``ChatMessagePrompt``s, upstream stream events become OpenAI-shaped completion
objects / stream chunks, so Hermes' chat_completions transport works unmodified.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import uuid
from types import SimpleNamespace
from typing import Any, Iterator

from agent.acp_openai_bridge import build_openai_tool_call

from . import _cascade
from ._proto import (
    CHAT_SOURCE_SYSTEM,
    CHAT_SOURCE_TOOL,
    CHAT_SOURCE_USER,
    STOP_REASON_MAX_TOKENS,
)

logger = logging.getLogger(__name__)

# ─── Credential resolution ───────────────────────────────────────────────────
#
# Hermes splits $HOME: the gateway process gets $HERMES_HOME while agent shell
# sessions get the profile home ($HERMES_HOME/home). A credential written by a
# login run in one context must still resolve in the other, so file lookups are
# tried against every home Hermes uses rather than relying on expanduser("~").

def _hermes_home() -> str:
    """$HERMES_HOME when set (always inside Hermes), else the stock ~/.hermes."""
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _home_candidates() -> list[str]:
    homes = [os.path.expanduser("~"), _hermes_home(), os.path.join(_hermes_home(), "home")]
    seen, out = set(), []
    for home in homes:
        if home not in seen:
            seen.add(home)
            out.append(home)
    return out


def _find_in_homes(rel_path: str) -> str | None:
    for home in _home_candidates():
        path = os.path.join(home, rel_path)
        if os.path.exists(path):
            return path
    return None


def resolve_credentials() -> tuple[str, str | None]:
    """``(session_token, api_server_url)`` from env, then the Devin CLI credential
    store, then the devin-gateway token file. Either element may be empty/None."""
    token = os.environ.get("DEVIN_API_KEY", "").strip()
    base_url = os.environ.get("DEVIN_BASE_URL", "").strip() or None
    if token and base_url:
        return token, base_url
    cli_credentials = _find_in_homes(".local/share/devin/credentials.toml")
    if cli_credentials:
        try:
            import tomllib

            with open(cli_credentials, "rb") as fh:
                data = tomllib.load(fh)
            if not token:
                token = str(data.get("windsurf_api_key") or "").strip()
            if not base_url:
                saved = str(data.get("api_server_url") or "").strip()
                base_url = saved or None
        except Exception:
            pass
    if not token:
        token_file = _find_in_homes(".devin-gateway/token")
        if token_file:
            try:
                with open(token_file, encoding="utf-8") as fh:
                    token = fh.read().strip()
            except OSError:
                pass
    return token, base_url


def bridge_credentials_to_env() -> None:
    """Mirror resolved credentials into env vars so Hermes' api_key-provider
    resolution (env-vars only) sees them. ``setdefault`` — explicit env wins."""
    token, base_url = resolve_credentials()
    if token:
        os.environ.setdefault("DEVIN_API_KEY", token)
    if base_url:
        os.environ.setdefault("DEVIN_BASE_URL", base_url)


# ─── OpenAI → Cascade conversion ─────────────────────────────────────────────

_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.DOTALL)


def _parse_data_url(url: str) -> dict | None:
    m = _DATA_URL_RE.match(url or "")
    return {"mime_type": m.group(1), "base64_data": m.group(2)} if m else None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _content_images(content: Any) -> list[dict]:
    if not isinstance(content, list):
        return []
    return [
        img
        for p in content
        if isinstance(p, dict) and p.get("type") == "image_url"
        for img in [_parse_data_url((p.get("image_url") or {}).get("url", ""))]
        if img
    ]


def _to_internal(messages: list[dict]) -> list[dict]:
    internal = []
    for msg in messages or ():
        role = msg.get("role")
        content = msg.get("content")
        if role == "tool":
            internal.append({
                "role": "tool",
                "content": content if isinstance(content, str) else json.dumps(content or ""),
                "tool_call_id": msg.get("tool_call_id"),
            })
        elif role == "assistant":
            tool_calls = [
                {
                    "id": tc.get("id", ""),
                    "name": (tc.get("function") or {}).get("name", ""),
                    "arguments_json": (tc.get("function") or {}).get("arguments") or "{}",
                }
                for tc in msg.get("tool_calls") or ()
                if isinstance(tc, dict)
            ]
            internal.append({
                "role": "assistant",
                "content": _content_text(content),
                "tool_calls": tool_calls or None,
                "thinking": msg.get("reasoning") or msg.get("reasoning_content"),
            })
        else:
            # user / system / developer — the gateway maps all non-tool,
            # non-assistant roles onto USER-source prompts.
            internal.append({
                "role": "user",
                "content": _content_text(content),
                "images": _content_images(content) or None,
            })
    return internal


def _system_prompt(messages: list[dict]) -> str:
    return "\n\n".join(
        _content_text(m.get("content"))
        for m in messages or ()
        if m.get("role") in ("system", "developer") and _content_text(m.get("content"))
    )


def _deterministic_uuid(seed: str) -> str:
    # FNV-1a over UTF-16 code units, matching devin-gateway's charCodeAt loop.
    h = 0x811C9DC5
    b = seed.encode("utf-16-le", errors="surrogatepass")
    for i in range(0, len(b), 2):
        h ^= b[i] | (b[i + 1] << 8)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}-0000-0000-0000-000000000000"


def _to_devin_prompts(internal: list[dict], cascade_id: str) -> list[dict]:
    prompts = []
    for index, msg in enumerate(internal):
        message_id = _deterministic_uuid(f"{cascade_id}\0{index}\0{msg['role']}")
        if msg["role"] == "assistant":
            prompts.append({
                "message_id": f"bot-{message_id}",
                "source": CHAT_SOURCE_SYSTEM,
                "prompt": msg["content"],
                "thinking": msg.get("thinking"),
                "tool_calls": msg.get("tool_calls"),
            })
        elif msg["role"] == "tool":
            prompts.append({
                "message_id": _deterministic_uuid(
                    f"{cascade_id}\0{index}\0tool\0{msg.get('tool_call_id') or ''}"
                ),
                "source": CHAT_SOURCE_TOOL,
                "tool_call_id": msg.get("tool_call_id"),
                "prompt": msg["content"],
            })
        else:
            prompts.append({
                "message_id": message_id,
                "source": CHAT_SOURCE_USER,
                "prompt": msg["content"],
                "images": msg.get("images"),
            })
    return prompts


def _tools_to_devin(tools: list[dict] | None) -> list[dict]:
    out = []
    for t in tools or ():
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({
            "name": fn["name"],
            "description": fn.get("description") or "",
            "json_schema_string": json.dumps(fn.get("parameters") or {"type": "object"}),
            "strict": False,
        })
    return out


def _map_tool_choice(choice: Any) -> dict | None:
    if not choice:
        return None
    if isinstance(choice, str):
        return {"option_name": "any" if choice == "required" else choice}
    if isinstance(choice, dict):
        fn = choice.get("function") or {}
        if choice.get("type") == "function" and fn.get("name"):
            return {"tool_name": fn["name"]}
    return None


def _finish_reason(stop_reason: int, has_tool_calls: bool) -> str:
    if has_tool_calls:
        return "tool_calls"
    return "length" if stop_reason == STOP_REASON_MAX_TOKENS else "stop"


def _usage_ns(usage: dict | None) -> Any:
    usage = usage or {}
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=int(usage.get("cache_read_tokens") or 0)),
    )


def _merge_tool_call(sink: list[dict], tc: dict) -> None:
    """Merge one upstream tool-call delta.

    Observed wire behavior: the first delta carries id+name with empty
    arguments; following deltas carry an EMPTY id and argument *fragments* that
    append to the pending call. Some upstreams instead re-send the same id with
    a cumulative arguments snapshot — handle both.
    """
    tid = tc.get("id") or ""
    name = tc.get("name") or ""
    args = tc.get("arguments_json") or tc.get("invalid_json_str") or ""
    if not tid:
        if sink:
            sink[-1]["arguments_json"] += args
        elif args:
            sink.append({"id": f"call_{len(sink)}", "name": name, "arguments_json": args})
        return
    for existing in sink:
        if existing["id"] != tid:
            continue
        if name:
            existing["name"] = name
        if args:
            cur = existing["arguments_json"]
            if not cur or args.startswith(cur):
                existing["arguments_json"] = args  # cumulative snapshot
            elif not cur.startswith(args):
                existing["arguments_json"] = cur + args  # fragment
        return
    sink.append({"id": tid, "name": name, "arguments_json": args})


def _chunk(model: str, *, delta: dict, finish_reason: str | None = None, usage: Any = None) -> Any:
    d = SimpleNamespace(
        role=delta.get("role"),
        content=delta.get("content"),
        tool_calls=delta.get("tool_calls"),
        reasoning_content=delta.get("reasoning"),
        reasoning=delta.get("reasoning"),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(index=0, delta=d, finish_reason=finish_reason)],
        model=model,
        usage=usage,
    )


# ─── Client ──────────────────────────────────────────────────────────────────


class DevinCascadeClient:
    """Minimal OpenAI-client-compatible facade for the Cascade Connect API."""

    # Already a complete client (never re-dispatch through a wire adapter) and
    # async-safe as-is — see agent/auxiliary_client.py.
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        **_: Any,
    ):
        file_token, file_base = resolve_credentials()
        self.api_key = (api_key or "").strip() or file_token
        self.base_url = (base_url or "").strip() or file_base or _cascade.DEVIN_API_URL
        self._default_headers = dict(default_headers or {})
        self._jwt_cache: dict = {}
        self.is_closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat_completion))

    def close(self) -> None:
        self.is_closed = True

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict] | None = None,
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        temperature: float | None = None,
        top_p: float | None = None,
        stop: Any = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> Any:
        if not self.api_key:
            raise RuntimeError(
                "Devin provider: no session token. Set DEVIN_API_KEY in ~/.hermes/.env, "
                "run `devin auth login`, or `bun run login` in devin-gateway."
            )
        model = (model or "").strip()
        stop_sequences = (
            [s for s in stop if isinstance(s, str)] if isinstance(stop, list)
            else [stop] if isinstance(stop, str) else None
        )
        cascade_id = str(uuid.uuid4())
        params = dict(
            api_key=self.api_key,
            model_uid=model,
            system_prompt=_system_prompt(messages or []),
            messages=_to_devin_prompts(_to_internal(messages or []), cascade_id),
            tools=_tools_to_devin(tools),
            tool_choice=_map_tool_choice(tool_choice),
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop_sequences=stop_sequences,
            cascade_id=cascade_id,
            base_url=self.base_url,
            timeout=float(timeout) if isinstance(timeout, (int, float)) else _cascade.DEFAULT_STREAM_TIMEOUT,
            jwt_cache=self._jwt_cache,
        )
        if stream:
            return self._stream_chunks(model, params)
        return self._complete(model, params)

    def _events(self, params: dict) -> Iterator[dict]:
        try:
            yield from _cascade.stream_chat(**params)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError(f"Devin API {exc.code} {exc.reason}: {detail}") from exc

    def _collect(self, params: dict) -> dict:
        """Drain the event stream into an assembled result."""
        text, thinking = [], []
        tool_calls: list[dict] = []
        stop_reason = 0
        usage = None
        for ev in self._events(params):
            kind = ev["type"]
            if kind == "text":
                text.append(ev["delta_text"])
            elif kind == "thinking":
                thinking.append(ev["delta_thinking"])
            elif kind == "toolcall":
                for tc in ev["tool_calls"]:
                    _merge_tool_call(tool_calls, tc)
            elif kind == "usage":
                usage = ev.get("usage") or usage
            elif kind == "done":
                stop_reason = ev.get("stop_reason") or 0
            elif kind == "error":
                raise RuntimeError(ev["error"])
        return {
            "text": "".join(text),
            "thinking": "".join(thinking),
            "tool_calls": tool_calls,
            "stop_reason": stop_reason,
            "usage": usage,
        }

    def _complete(self, model: str, params: dict) -> Any:
        r = self._collect(params)
        tool_calls = [
            build_openai_tool_call(call_id=tc["id"] or f"call_{i}", name=tc["name"],
                                   arguments=tc["arguments_json"])
            for i, tc in enumerate(r["tool_calls"])
        ]
        message = SimpleNamespace(
            content=r["text"] or None,
            tool_calls=tool_calls or None,
            reasoning=r["thinking"] or None,
            reasoning_content=r["thinking"] or None,
            reasoning_details=None,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=message,
                finish_reason=_finish_reason(r["stop_reason"], bool(tool_calls)),
            )],
            usage=_usage_ns(r["usage"]),
            model=model,
        )

    def _stream_chunks(self, model: str, params: dict) -> Iterator[Any]:
        """Yield OpenAI-shaped chunks. Tool calls are buffered and emitted once,
        complete, with the finish chunk — Cascade sends cumulative argument
        snapshots, which must not be concatenated by the delta accumulator."""
        role_sent = False
        tool_calls: list[dict] = []
        stop_reason = 0
        usage = None
        for ev in self._events(params):
            kind = ev["type"]
            if kind == "text":
                yield _chunk(model, delta={
                    "role": None if role_sent else "assistant",
                    "content": ev["delta_text"],
                })
                role_sent = True
            elif kind == "thinking":
                yield _chunk(model, delta={
                    "role": None if role_sent else "assistant",
                    "reasoning": ev["delta_thinking"],
                })
                role_sent = True
            elif kind == "toolcall":
                for tc in ev["tool_calls"]:
                    _merge_tool_call(tool_calls, tc)
            elif kind == "usage":
                usage = ev.get("usage") or usage
            elif kind == "done":
                stop_reason = ev.get("stop_reason") or 0
            elif kind == "error":
                raise RuntimeError(ev["error"])

        tool_deltas = [
            SimpleNamespace(
                index=i,
                id=tc["id"],
                type="function",
                function=SimpleNamespace(name=tc["name"], arguments=tc["arguments_json"]),
            )
            for i, tc in enumerate(tool_calls)
        ]
        yield _chunk(
            model,
            delta={"role": None if role_sent else "assistant", "tool_calls": tool_deltas or None},
            finish_reason=_finish_reason(stop_reason, bool(tool_calls)),
        )
        yield SimpleNamespace(choices=[], model=model, usage=_usage_ns(usage))
