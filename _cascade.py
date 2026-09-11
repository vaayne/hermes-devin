"""Devin / Codeium Cascade Connect-RPC client.

Port of devin-gateway's src/devin.ts. Two RPCs over HTTP/1.1:

  1. GetUserJwt      — exchange the session token (apiKey) for a per-user JWT.
  2. GetChatMessage  — streaming chat via the Connect protocol (protobuf frames).

The session token is the value from `devin auth login` / the gateway's OAuth
flow, prefixed with ``devin-session-token$`` if not already.
"""

from __future__ import annotations

import gzip
import http.client
import json
import logging
import urllib.request
import uuid
from typing import Any, Iterator

from hermes_cli.urllib_security import open_credentialed_url

from ._proto import (
    CACHE_CONTROL_EPHEMERAL,  # noqa: F401  (re-exported for callers)
    PLANNER_MODE_DEFAULT,
    REQUEST_TYPE_CASCADE,
    ProtoEncoder,
    decode_get_chat_message_response,
    decode_get_user_jwt_response,
    encode_get_chat_message_request,
    encode_get_cli_model_configs_request,
    encode_get_user_jwt_request,
    parse_cli_model_configs,
)

logger = logging.getLogger(__name__)

DEVIN_API_URL = "https://server.codeium.com"
AUTH_PATH = "/exa.auth_pb.AuthService/GetUserJwt"
CHAT_MESSAGE_PATH = "/exa.api_server_pb.ApiServerService/GetChatMessage"
MODEL_CONFIGS_PATH = "/exa.api_server_pb.ApiServerService/GetCliModelConfigs"
DEVIN_IDE_VERSION = "3.2.23"
DEVIN_EXTENSION_VERSION = "1.48.2"
SESSION_TOKEN_PREFIX = "devin-session-token$"

CONNECT_COMPRESSED_FLAG = 0x01
CONNECT_END_STREAM_FLAG = 0x02
MAX_FRAME_PAYLOAD = 16 * 1024 * 1024

DEFAULT_STOP_PATTERNS = ["\n\nUSER:", "\n\nASSISTANT:", "<|context_request|>", "<|end_of_turn|>"]
AUTH_TIMEOUT = 30.0
# Max silence from the upstream chat stream before aborting; urllib's read
# timeout fires per read(), so it measures exactly this.
DEFAULT_STREAM_TIMEOUT = 300.0


def normalize_token(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return token
    return token if token.startswith(SESSION_TOKEN_PREFIX) else f"{SESSION_TOKEN_PREFIX}{token}"


def build_metadata(api_key: str, user_jwt: str | None = None) -> dict:
    return {
        "ide_name": "windsurf",
        "ide_version": DEVIN_IDE_VERSION,
        "extension_name": "windsurf",
        "extension_version": DEVIN_EXTENSION_VERSION,
        "api_key": api_key,
        "locale": "en",
        "user_jwt": user_jwt,
    }


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float):
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    return open_credentialed_url(req, timeout=timeout)


def get_user_jwt(api_key: str, base_url: str = DEVIN_API_URL, timeout: float = AUTH_TIMEOUT) -> dict:
    """Exchange the session token for a user JWT. Returns {"user_jwt", "base_url"}."""
    token = normalize_token(api_key)
    body = encode_get_user_jwt_request(build_metadata(token))
    url = base_url.rstrip("/") + AUTH_PATH
    with _post(
        url,
        body,
        {"content-type": "application/proto", "connect-protocol-version": "1", "accept": "*/*"},
        timeout,
    ) as resp:
        payload = resp.read()
    try:
        decoded = decode_get_user_jwt_response(payload)
    except Exception:
        decoded = decode_get_user_jwt_response(gzip.decompress(payload))
    if not decoded["user_jwt"]:
        raise RuntimeError("Devin auth: empty user JWT")
    custom = decoded["custom_api_server_url"].strip().rstrip("/")
    return {"user_jwt": decoded["user_jwt"], "base_url": custom or None}


def stream_chat(
    *,
    api_key: str,
    model_uid: str,
    system_prompt: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    stop_sequences: list[str] | None = None,
    cascade_id: str | None = None,
    tool_choice: dict | None = None,
    base_url: str | None = None,
    timeout: float = DEFAULT_STREAM_TIMEOUT,
    jwt_cache: dict | None = None,
) -> Iterator[dict]:
    """Yield events: {"type": "text"|"thinking"|"toolcall"|"usage"|"done"|"error", ...}."""
    token = normalize_token(api_key)
    base = (base_url or DEVIN_API_URL).rstrip("/")

    # JWTs are short-lived server-side but stable within a burst of requests;
    # cache for 5 minutes to skip the handshake on back-to-back turns.
    import time

    cache_key = f"{token}@{base}"
    auth = None
    if jwt_cache is not None:
        cached = jwt_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            auth = cached[1]
    if auth is None:
        auth = get_user_jwt(token, base, timeout=min(timeout, AUTH_TIMEOUT))
        if jwt_cache is not None:
            jwt_cache[cache_key] = (time.monotonic(), auth)
    chat_base = auth["base_url"] or base

    cascade_id = cascade_id or str(uuid.uuid4())
    # Codeium's upstream rejects temperature=0 with invalid_argument for some
    # models (proto3 omits the field entirely). Clamp to a negligible positive
    # value that is indistinguishable from deterministic output.
    temp = 0.01 if temperature == 0 else (temperature if temperature is not None else 0.4)

    request = {
        "metadata": build_metadata(token, auth["user_jwt"]),
        "prompt": system_prompt,
        "chat_message_prompts": messages,
        "chat_model_uid": model_uid,
        "configuration": {
            "num_completions": 1,
            "max_tokens": max_tokens or 64000,
            "max_newlines": 200,
            "temperature": temp,
            "first_temperature": temp,
            "top_k": 50,
            "top_p": top_p if top_p is not None else 1,
            "stop_patterns": DEFAULT_STOP_PATTERNS + list(stop_sequences or ()),
            "fim_eot_prob_threshold": 1,
        },
        "tools": tools,
        "disable_parallel_tool_calls": True,
        "tool_choice": tool_choice or {"option_name": "auto"},
        "cascade_id": cascade_id,
        "execution_id": str(uuid.uuid4()),
    }

    req_bytes = encode_get_chat_message_request(request)
    gz = gzip.compress(req_bytes)
    frame = bytes([CONNECT_COMPRESSED_FLAG]) + len(gz).to_bytes(4, "big") + gz

    resp = _post(
        chat_base + CHAT_MESSAGE_PATH,
        frame,
        {
            "content-type": "application/connect+proto",
            "connect-protocol-version": "1",
            "connect-content-encoding": "gzip",
            "accept-encoding": "identity",
            "user-agent": "connect-go/1.18.1 (go1.26.3)",
            "connect-accept-encoding": "gzip",
        },
        timeout,
    )

    pending = bytearray()
    last_stop_reason = 0
    last_usage = None

    with resp:
        while True:
            try:
                chunk = resp.read(65536)
            except http.client.IncompleteRead as exc:
                # Upstream sometimes closes the socket right after the
                # end-stream trailer without a terminating chunk.
                chunk = exc.partial or b""
                done = True
            except (http.client.RemoteDisconnected, ConnectionError):
                chunk = b""
                done = True
            else:
                done = not chunk
            pending += chunk
            while len(pending) >= 5:
                flag = pending[0]
                length = int.from_bytes(pending[1:5], "big")
                if length > MAX_FRAME_PAYLOAD:
                    raise RuntimeError(f"Connect frame length {length} exceeds {MAX_FRAME_PAYLOAD} bytes")
                if len(pending) < 5 + length:
                    break
                payload = bytes(pending[5 : 5 + length])
                del pending[: 5 + length]

                if flag & CONNECT_END_STREAM_FLAG:
                    trailer_raw = gzip.decompress(payload) if flag & CONNECT_COMPRESSED_FLAG else payload
                    trailer = trailer_raw.decode("utf-8", errors="replace").strip()
                    if trailer:
                        try:
                            parsed = json.loads(trailer)
                        except Exception:
                            parsed = None
                        error = (parsed or {}).get("error") or {}
                        if error.get("code"):
                            stream_error = (
                                f"Devin stream error {error['code']}: {error.get('message', '')}"
                            )
                            yield {"type": "error", "error": stream_error, "code": error["code"]}
                    continue

                raw = gzip.decompress(payload) if flag & CONNECT_COMPRESSED_FLAG else payload
                msg = decode_get_chat_message_response(raw)

                if msg["delta_text"]:
                    yield {"type": "text", "delta_text": msg["delta_text"]}
                if msg["delta_thinking"]:
                    yield {
                        "type": "thinking",
                        "delta_thinking": msg["delta_thinking"],
                        "delta_signature": msg.get("delta_signature") or "",
                    }
                if msg["delta_tool_calls"]:
                    yield {"type": "toolcall", "tool_calls": msg["delta_tool_calls"]}
                if msg["usage"]:
                    last_usage = msg["usage"]
                    yield {"type": "usage", "usage": msg["usage"]}
                if msg["stop_reason"]:
                    last_stop_reason = msg["stop_reason"]
            if done:
                break

    yield {"type": "done", "stop_reason": last_stop_reason, "usage": last_usage}


def discover_models(
    api_key: str, base_url: str = DEVIN_API_URL, timeout: float = 8.0
) -> list[dict]:
    """Fetch the live Cascade model catalog. Raises on transport errors."""
    token = normalize_token(api_key)
    body = encode_get_cli_model_configs_request(build_metadata(token))
    url = base_url.rstrip("/") + MODEL_CONFIGS_PATH
    with _post(
        url,
        body,
        {"content-type": "application/proto", "connect-protocol-version": "1", "accept": "*/*"},
        timeout,
    ) as resp:
        data = resp.read()
    try:
        return parse_cli_model_configs(data)
    except Exception as exc:
        logger.warning("devin: failed to parse model configs: %s", exc)
        return []
