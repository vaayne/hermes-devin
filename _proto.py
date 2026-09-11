"""Minimal protobuf binary encode/decode for the Devin/Codeium Cascade Connect API.

Port of devin-gateway's src/proto.ts. Only the message shapes and field numbers
the client uses are implemented. Proto3 semantics: zero-valued scalar fields are
omitted; repeated fields are emitted as repeated tags (no packed encoding).
"""

from __future__ import annotations

import struct
from typing import Any, Callable, Iterable

# Enum constants (proto3 numeric values)
CHAT_SOURCE_UNSPECIFIED = 0
CHAT_SOURCE_USER = 1
CHAT_SOURCE_SYSTEM = 2
CHAT_SOURCE_TOOL = 4

STOP_REASON_UNSPECIFIED = 0
STOP_REASON_MAX_TOKENS = 3
STOP_REASON_FUNCTION_CALL = 10

REQUEST_TYPE_CASCADE = 5
PLANNER_MODE_DEFAULT = 1
CACHE_CONTROL_EPHEMERAL = 1


class ProtoEncoder:
    def __init__(self) -> None:
        self._buf = bytearray()

    def _varint(self, n: int) -> None:
        n &= 0xFFFFFFFFFFFFFFFF
        while n > 0x7F:
            self._buf.append((n & 0x7F) | 0x80)
            n >>= 7
        self._buf.append(n)

    def _tag(self, field: int, wire: int) -> None:
        self._varint((field << 3) | wire)

    def string(self, field: int, value: str | None) -> None:
        if not value:
            return
        data = value.encode("utf-8")
        self._tag(field, 2)
        self._varint(len(data))
        self._buf += data

    def uint32(self, field: int, value: int | None) -> None:
        if not value:
            return
        self._tag(field, 0)
        self._varint(value)

    uint64 = uint32

    def bool(self, field: int, value: bool | None) -> None:
        if not value:
            return
        self._tag(field, 0)
        self._varint(1)

    def double(self, field: int, value: float | None) -> None:
        if not value:
            return
        self._tag(field, 1)
        self._buf += struct.pack("<d", value)

    def message(self, field: int, encode: Callable[["ProtoEncoder"], None]) -> None:
        sub = ProtoEncoder()
        encode(sub)
        data = sub.finish()
        self._tag(field, 2)
        self._varint(len(data))
        self._buf += data

    def repeated_message(
        self, field: int, values: Iterable[Any] | None, encode: Callable[["ProtoEncoder", Any], None]
    ) -> None:
        for v in values or ():
            self.message(field, lambda e, v=v: encode(e, v))

    def repeated_string(self, field: int, values: Iterable[str] | None) -> None:
        for v in values or ():
            self.string(field, v)

    def finish(self) -> bytes:
        return bytes(self._buf)


class ProtoDecoder:
    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        self._bytes = bytes(data)
        self.pos = 0

    def read_varint(self) -> int:
        result = 0
        shift = 0
        while self.pos < len(self._bytes):
            byte = self._bytes[self.pos]
            self.pos += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        return result

    def read_tag(self) -> tuple[int, int]:
        tag = self.read_varint()
        return tag >> 3, tag & 0x07

    def read_bytes(self) -> bytes:
        length = self.read_varint()
        start = self.pos
        self.pos += length
        return self._bytes[start : start + length]

    def read_string(self) -> str:
        return self.read_bytes().decode("utf-8", errors="replace")

    def skip(self, wire: int) -> None:
        if wire == 0:
            self.read_varint()
        elif wire == 1:
            self.pos += 8
        elif wire == 2:
            # NOTE: `self.pos += self.read_varint()` would read the stale pos —
            # Python evaluates the LHS target before calling the function.
            length = self.read_varint()
            self.pos += length
        elif wire == 5:
            self.pos += 4
        else:
            raise ValueError(f"Unknown wire type: {wire}")

    @property
    def done(self) -> bool:
        return self.pos >= len(self._bytes)

    def read_message(self, fn: Callable[["ProtoDecoder"], Any]) -> Any:
        return fn(ProtoDecoder(self.read_bytes()))


# ─── Domain message codecs ──────────────────────────────────────────────────


def encode_metadata(e: ProtoEncoder, m: dict) -> None:
    e.string(1, m.get("ide_name"))
    e.string(7, m.get("ide_version"))
    e.string(12, m.get("extension_name"))
    e.string(2, m.get("extension_version"))
    e.string(3, m.get("api_key"))
    e.string(4, m.get("locale"))
    e.string(21, m.get("user_jwt"))


def encode_get_user_jwt_request(metadata: dict) -> bytes:
    enc = ProtoEncoder()
    enc.message(1, lambda e: encode_metadata(e, metadata))
    return enc.finish()


def decode_get_user_jwt_response(data: bytes) -> dict:
    d = ProtoDecoder(data)
    res = {"user_jwt": "", "custom_api_server_url": ""}
    while not d.done:
        field, wire = d.read_tag()
        if wire != 2:
            d.skip(wire)
            continue
        if field == 1:
            res["user_jwt"] = d.read_string()
        elif field == 2:
            res["custom_api_server_url"] = d.read_string()
        else:
            d.skip(wire)
    return res


def _encode_chat_tool_call(e: ProtoEncoder, tc: dict) -> None:
    e.string(1, tc.get("id"))
    e.string(2, tc.get("name"))
    e.string(3, tc.get("arguments_json"))
    e.string(4, tc.get("invalid_json_str"))
    e.string(5, tc.get("invalid_json_err"))
    e.bool(6, tc.get("is_custom_tool_call"))


def decode_chat_tool_call(d: ProtoDecoder) -> dict:
    tc: dict[str, Any] = {"id": "", "name": "", "arguments_json": ""}
    while not d.done:
        field, wire = d.read_tag()
        if field == 1:
            tc["id"] = d.read_string()
        elif field == 2:
            tc["name"] = d.read_string()
        elif field == 3:
            tc["arguments_json"] = d.read_string()
        elif field == 4:
            tc["invalid_json_str"] = d.read_string()
        elif field == 5:
            tc["invalid_json_err"] = d.read_string()
        elif field == 6:
            tc["is_custom_tool_call"] = d.read_varint() != 0
        else:
            d.skip(wire)
    return tc


def _encode_image_data(e: ProtoEncoder, img: dict) -> None:
    e.string(1, img.get("base64_data"))
    e.string(2, img.get("mime_type"))


def encode_chat_message_prompt(e: ProtoEncoder, p: dict) -> None:
    e.string(1, p.get("message_id"))
    e.uint32(2, p.get("source"))
    e.string(3, p.get("prompt"))
    e.repeated_message(6, p.get("tool_calls"), _encode_chat_tool_call)
    e.string(7, p.get("tool_call_id"))
    e.bool(9, p.get("tool_result_is_error"))
    e.repeated_message(10, p.get("images"), _encode_image_data)
    e.string(11, p.get("thinking"))
    e.string(12, p.get("signature"))
    e.string(18, p.get("signature_type"))


def _encode_chat_tool_definition(e: ProtoEncoder, t: dict) -> None:
    e.string(1, t.get("name"))
    e.string(2, t.get("description"))
    e.string(3, t.get("json_schema_string"))
    e.bool(12, t.get("strict"))


def _encode_chat_tool_choice(e: ProtoEncoder, c: dict) -> None:
    e.string(1, c.get("option_name"))
    e.string(2, c.get("tool_name"))


def _encode_completion_configuration(e: ProtoEncoder, c: dict) -> None:
    e.uint64(1, c.get("num_completions"))
    e.uint64(2, c.get("max_tokens"))
    e.uint64(3, c.get("max_newlines"))
    e.double(5, c.get("temperature"))
    e.double(6, c.get("first_temperature"))
    e.uint64(7, c.get("top_k"))
    e.double(8, c.get("top_p"))
    e.repeated_string(9, c.get("stop_patterns"))
    e.double(11, c.get("fim_eot_prob_threshold"))


def encode_get_chat_message_request(r: dict) -> bytes:
    enc = ProtoEncoder()
    enc.message(1, lambda e: encode_metadata(e, r["metadata"]))
    enc.string(2, r.get("prompt"))
    enc.repeated_message(3, r.get("chat_message_prompts"), encode_chat_message_prompt)
    enc.string(21, r.get("chat_model_uid"))
    enc.uint32(7, REQUEST_TYPE_CASCADE)
    enc.message(8, lambda e: _encode_completion_configuration(e, r["configuration"]))
    enc.repeated_message(10, r.get("tools"), _encode_chat_tool_definition)
    enc.bool(11, r.get("disable_parallel_tool_calls"))
    enc.message(12, lambda e: _encode_chat_tool_choice(e, r.get("tool_choice") or {}))
    enc.message(13, lambda e: e.uint32(1, CACHE_CONTROL_EPHEMERAL))
    enc.string(16, r.get("cascade_id"))
    enc.uint32(20, PLANNER_MODE_DEFAULT)
    enc.string(22, r.get("execution_id"))
    return enc.finish()


def decode_model_usage_stats(d: ProtoDecoder) -> dict:
    s: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
    }
    while not d.done:
        field, wire = d.read_tag()
        if field == 2:
            s["input_tokens"] = d.read_varint()
        elif field == 3:
            s["output_tokens"] = d.read_varint()
        elif field == 4:
            s["cache_write_tokens"] = d.read_varint()
        elif field == 5:
            s["cache_read_tokens"] = d.read_varint()
        elif field == 7:
            s["message_id"] = d.read_string()
        elif field == 9:
            s["model_uid"] = d.read_string()
        else:
            d.skip(wire)
    return s


def decode_get_chat_message_response(data: bytes) -> dict:
    d = ProtoDecoder(data)
    res: dict[str, Any] = {
        "message_id": "",
        "delta_text": "",
        "stop_reason": 0,
        "delta_tool_calls": [],
        "usage": None,
        "delta_thinking": "",
        "delta_signature": "",
    }
    while not d.done:
        field, wire = d.read_tag()
        if field == 1:
            res["message_id"] = d.read_string()
        elif field == 3:
            res["delta_text"] = d.read_string()
        elif field == 5:
            res["stop_reason"] = d.read_varint()
        elif field == 6:
            res["delta_tool_calls"].append(d.read_message(decode_chat_tool_call))
        elif field == 7:
            res["usage"] = d.read_message(decode_model_usage_stats)
        elif field == 8:
            res["redact"] = d.read_varint() != 0
        elif field == 9:
            res["delta_thinking"] = d.read_string()
        elif field == 10:
            res["delta_signature"] = d.read_string()
        elif field == 11:
            res["thinking_redacted"] = d.read_varint() != 0
        elif field == 14:
            res["credit_cost"] = d.read_varint()
        elif field == 15:
            res["output_id"] = d.read_string()
        elif field == 17:
            res["request_id"] = d.read_string()
        elif field == 21:
            res["delta_signature_type"] = d.read_string()
        elif field == 23:
            res["actual_model_uid"] = d.read_string()
        else:
            d.skip(wire)
    return res


# ─── GetCliModelConfigs (model discovery) ────────────────────────────────────


def encode_get_cli_model_configs_request(metadata: dict) -> bytes:
    enc = ProtoEncoder()
    enc.message(1, lambda e: encode_metadata(e, metadata))
    return enc.finish()


def _parse_model_features_thinking(d: ProtoDecoder) -> bool:
    while not d.done:
        field, wire = d.read_tag()
        if field == 15 and wire == 0:
            return d.read_varint() != 0
        d.skip(wire)
    return False


def _parse_model_info_thinking(d: ProtoDecoder) -> bool:
    while not d.done:
        field, wire = d.read_tag()
        if field == 6 and wire == 2:
            return d.read_message(_parse_model_features_thinking)
        d.skip(wire)
    return False


def _parse_client_model_config(d: ProtoDecoder) -> dict | None:
    import re

    model_id = ""
    label = ""
    disabled = False
    configured_max_tokens = 0
    supports_images = False
    supports_thinking = False
    while not d.done:
        field, wire = d.read_tag()
        if field == 1 and wire == 2:
            label = d.read_string()
        elif field == 4 and wire == 0:
            disabled = d.read_varint() != 0
        elif field == 5 and wire == 0:
            supports_images = d.read_varint() != 0
        elif field == 18 and wire == 0:
            configured_max_tokens = d.read_varint()
        elif field == 22 and wire == 2:
            model_id = d.read_string()
        elif field == 23 and wire == 2:
            supports_thinking = d.read_message(_parse_model_info_thinking)
        else:
            d.skip(wire)

    if disabled or not model_id.strip():
        return None
    reasoning = not re.search(r"\bno thinking\b", label, re.I) and (
        supports_thinking
        or bool(re.search(r"think|thinking|minimal|high|medium|low|xhigh|max|reasoning", label, re.I))
    )
    context_window = configured_max_tokens or 200_000
    return {
        "id": model_id.strip(),
        "name": label.strip() or model_id.strip(),
        "context_window": context_window,
        "max_tokens": min(configured_max_tokens or 64_000, 64_000),
        "reasoning": reasoning,
        "supports_images": supports_images,
    }


def parse_cli_model_configs(data: bytes) -> list[dict]:
    models = []
    d = ProtoDecoder(data)
    while not d.done:
        field, wire = d.read_tag()
        if field == 1 and wire == 2:
            model = d.read_message(_parse_client_model_config)
            if model:
                models.append(model)
        else:
            d.skip(wire)
    return models
