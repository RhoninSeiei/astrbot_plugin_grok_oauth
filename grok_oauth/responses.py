"""Stateless Responses encoding and terminal-aware response normalization."""

import copy
import json
import re
from dataclasses import dataclass, field, replace

from .continuation import decode_envelope, restore_continuation
from .errors import EmptyOutput, GrokOAuthError, ProtocolError, StreamIncomplete
from .search import collect_citations, collect_search_sources
from .sse import iter_json_sse_events
from .token_usage import safe_token_usage


@dataclass(frozen=True)
class ResponseResult:
    id: str
    model: str
    status: str
    text: str
    output_items: list[dict] = field(default_factory=list, repr=False)
    reasoning_items: list[dict] = field(default_factory=list, repr=False)
    function_calls: list[dict] = field(default_factory=list, repr=False)
    native_items: list[dict] = field(default_factory=list, repr=False)
    usage: dict | None = field(default=None, repr=False)
    partial: bool = False
    citations: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ResponseEvent:
    kind: str
    delta: str = field(default="", repr=False)
    item_id: str = ""
    result: ResponseResult | None = field(default=None, repr=False)


def _required_string(value, name):
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"Missing or invalid {name}")
    return value


def encode_messages(messages: list[dict]) -> list[dict]:
    """Convert chat history without mutating input or dropping tool call IDs."""
    result = []
    for message in messages:
        if not isinstance(message, dict):
            raise ProtocolError("History message must be an object")
        role = message.get("role")
        if role == "tool":
            content = message.get("content", "")
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": _required_string(message.get("tool_call_id"), "call_id"),
                    "output": content
                    if isinstance(content, str)
                    else json.dumps(content, ensure_ascii=False),
                }
            )
            continue
        if role not in {"user", "assistant", "system", "developer"}:
            raise ProtocolError("Unsupported history role")
        replay = restore_continuation(message)
        if replay is not None:
            result.extend(replay)
            continue
        content = message.get("content")
        parts = []
        reasoning = []
        if isinstance(content, str):
            if content:
                parts.append(
                    {
                        "type": "input_text" if role != "assistant" else "output_text",
                        "text": content,
                    }
                )
        elif content is not None:
            if not isinstance(content, list):
                raise ProtocolError("Invalid message content")
            for part in content:
                if not isinstance(part, dict):
                    raise ProtocolError("Invalid content part")
                kind = part.get("type")
                if kind in {"text", "input_text", "output_text"}:
                    if not isinstance(part.get("text"), str):
                        raise ProtocolError("Invalid text content")
                    parts.append(
                        {
                            "type": "output_text" if role == "assistant" else "input_text",
                            "text": part["text"],
                        }
                    )
                elif kind in {"image_url", "input_image"} and role == "user":
                    image = part.get("image_url")
                    url = image.get("url") if isinstance(image, dict) else image
                    parts.append(
                        {"type": "input_image", "image_url": _required_string(url, "image_url")}
                    )
                elif kind == "think" and role == "assistant":
                    encrypted = part.get("encrypted")
                    if encrypted:
                        state = decode_envelope(encrypted)
                        if state is None:
                            continue
                        if state.get("version") == 2:
                            # The host edited this turn: do not reuse stale private state.
                            continue
                        items = state.get("items")
                        if not isinstance(items, list) or any(
                            not isinstance(x, dict) or x.get("type") != "reasoning" for x in items
                        ):
                            raise ProtocolError("Invalid saved reasoning items")
                        reasoning.extend(copy.deepcopy(items))
                else:
                    raise ProtocolError("Unsupported message content part")
        result.extend(reasoning)
        if parts:
            if role == "assistant":
                result.append({"role": role, "content": "".join(part["text"] for part in parts)})
            else:
                result.append({"role": role, "content": parts})
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list) or (calls and role != "assistant"):
            raise ProtocolError("Invalid history tool calls")
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise ProtocolError("Invalid history function call")
            function = call["function"]
            arguments = function.get("arguments")
            _validate_arguments(arguments)
            result.append(
                {
                    "type": "function_call",
                    "call_id": _required_string(call.get("id"), "call_id"),
                    "name": _required_string(function.get("name"), "function name"),
                    "arguments": arguments,
                }
            )
    return result


def _validate_arguments(arguments):
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else None
    except (ValueError, TypeError):
        raise ProtocolError("Invalid function arguments") from None
    if not isinstance(parsed, dict):
        raise ProtocolError("Function arguments must be a JSON object")
    return parsed


def _normalize_response(raw: dict) -> ResponseResult:
    """Accept only a complete response with usable text, functions or native items."""
    if not isinstance(raw, dict):
        raise ProtocolError("Response must be an object")
    status = raw.get("status")
    if not isinstance(status, str):
        raise ProtocolError("Invalid response status")
    if status != "completed":
        if status in {"incomplete", "in_progress", "cancelled"}:
            raise StreamIncomplete("Response did not complete", partial=bool(raw.get("output")))
        raise ProtocolError("Response failed or has invalid status")
    output = raw.get("output")
    if not isinstance(output, list):
        raise ProtocolError("Response output must be a list")
    text, reasoning, functions, native = [], [], [], []
    citations = []
    for item in output:
        if not isinstance(item, dict):
            raise ProtocolError("Invalid response output item")
        kind = item.get("type")
        if kind == "message":
            if not isinstance(item.get("content"), list):
                raise ProtocolError("Invalid output message")
            for content in item["content"]:
                if not isinstance(content, dict):
                    raise ProtocolError("Invalid output content")
                if content.get("type") in {"output_text", "refusal"}:
                    value = (
                        content.get("text")
                        if content["type"] == "output_text"
                        else content.get("refusal")
                    )
                    if not isinstance(value, str):
                        raise ProtocolError("Invalid output text")
                    text.append(value)
                    collect_citations(content, citations)
        elif kind == "reasoning":
            normalized = copy.deepcopy(item)
            for key in ("summary", "content"):
                values = normalized.get(key)
                if values is None:
                    continue
                if not isinstance(values, list) or any(
                    not isinstance(value, dict) for value in values
                ):
                    raise ProtocolError("Invalid reasoning content")
            reasoning.append(normalized)
        elif kind == "function_call":
            _required_string(item.get("call_id"), "call_id")
            _required_string(item.get("name"), "function name")
            _validate_arguments(item.get("arguments"))
            functions.append(copy.deepcopy(item))
        elif isinstance(kind, str) and kind.endswith("_call"):
            native.append(copy.deepcopy(item))
    for item in native:
        if item.get("type") == "web_search_call":
            collect_search_sources(item, citations)
    if not (any(text) or functions or native):
        raise EmptyOutput("Response contains no usable output")
    usage = raw.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise ProtocolError("Invalid usage")
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            if name in usage and (type(usage[name]) is not int or usage[name] < 0):
                raise ProtocolError("Invalid token count")
        details = usage.get("input_tokens_details") or {}
        if not isinstance(details, dict):
            raise ProtocolError("Invalid cached token usage")
        cached = details.get("cached_tokens")
        if cached is not None and (
            type(cached) is not int or cached < 0 or cached > usage.get("input_tokens", cached)
        ):
            raise ProtocolError("Invalid cached token count")
    return ResponseResult(
        id=_required_string(raw.get("id"), "response id"),
        model=str(raw.get("model") or ""),
        status=status,
        text="".join(text),
        output_items=copy.deepcopy(output),
        reasoning_items=reasoning,
        function_calls=functions,
        native_items=native,
        citations=citations,
        usage=safe_token_usage(usage),
    )


def normalize_response(raw: dict) -> ResponseResult:
    """Normalize output while retaining safe reported usage on rejection."""
    try:
        return _normalize_response(raw)
    except GrokOAuthError as error:
        error.token_usage = safe_token_usage(raw.get("usage") if isinstance(raw, dict) else None)
        raise


class ResponsesClient:
    def __init__(self, http, *, max_event_bytes=2 * 1024 * 1024, usage_observer=None):
        self.http = http
        self.max_event_bytes = max_event_bytes
        self._usage_observer = usage_observer

    def _observe_usage(self, usage, *, response_id=None, complete=False):
        """Publish at parse time, before downstream queues can hide a final result."""
        if self._usage_observer is None or usage is None:
            return
        if not isinstance(response_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", response_id
        ):
            response_id = None
        try:
            self._usage_observer(copy.deepcopy(usage), response_id=response_id, complete=complete)
        except Exception:
            # Accounting observers must not change chat or retry semantics.
            pass

    @staticmethod
    def _payload(payload, stream):
        if (
            payload.get("store") not in (None, False)
            or "previous_response_id" in payload
            or "conversation" in payload
        ):
            raise ProtocolError("Server-side conversation storage is disabled")
        result = copy.deepcopy(payload)
        result["store"] = False
        result["stream"] = stream
        return result

    async def create(self, payload, *, policy):
        raw = await self.http.request_json(
            "POST", "/responses", json=self._payload(payload, False), policy=policy
        )
        if isinstance(raw, dict):
            self._observe_usage(
                safe_token_usage(raw.get("usage")),
                response_id=raw.get("id"),
                complete=raw.get("status") in ("completed", "failed", "incomplete", "cancelled"),
            )
        return normalize_response(raw)

    async def stream(self, payload, *, policy):
        partial = False
        token_usage = None
        try:
            async with self.http.stream_sse(
                "/responses", json=self._payload(payload, True), policy=policy
            ) as chunks:
                async for event in iter_json_sse_events(
                    chunks, max_event_bytes=self.max_event_bytes
                ):
                    # Usage events contain cumulative snapshots, not increments.
                    # Keep only the latest reported snapshot and never sum them.
                    response = event.get("response")
                    observed = safe_token_usage(
                        response.get("usage") if isinstance(response, dict) else None
                    ) or safe_token_usage(event.get("usage"))
                    if observed is not None:
                        token_usage = observed
                    kind = event.get("type")
                    if observed is not None:
                        self._observe_usage(
                            observed,
                            response_id=response.get("id")
                            if isinstance(response, dict)
                            else event.get("response_id"),
                            complete=kind
                            in {
                                "response.completed",
                                "response.failed",
                                "response.incomplete",
                                "error",
                            },
                        )
                    if kind == "response.completed":
                        result = normalize_response(response)
                        if result.usage is None and token_usage is not None:
                            # Earlier snapshots report real consumption, but may
                            # precede the final work done by this response.
                            result = replace(
                                result,
                                usage={**copy.deepcopy(token_usage), "is_partial": True},
                            )
                        token_usage = result.usage
                        yield ResponseEvent("completed", result=result)
                        return
                    if kind in {"response.failed", "response.incomplete", "error"}:
                        raise StreamIncomplete("Upstream stream did not complete", partial=partial)
                    mapping = {
                        "response.output_text.delta": "text_delta",
                        "response.reasoning_text.delta": "reasoning_delta",
                        "response.reasoning_summary_text.delta": "reasoning_delta",
                        "response.function_call_arguments.delta": "function_delta",
                    }
                    if kind in mapping:
                        delta = event.get("delta")
                        if not isinstance(delta, str):
                            raise ProtocolError("Invalid stream delta", partial=partial)
                        partial = partial or bool(delta)
                        yield ResponseEvent(
                            mapping[kind], delta=delta, item_id=str(event.get("item_id") or "")
                        )
            raise StreamIncomplete("Missing response.completed event", partial=partial)
        except BaseException as error:
            if isinstance(error, GrokOAuthError):
                error.partial = error.partial or partial
            error.token_usage = token_usage
            raise
