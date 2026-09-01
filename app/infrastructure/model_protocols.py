"""Provider protocol adapters for the model gateway.

The gateway owns transport, budgeting, and the tool loop.  Everything that
differs between OpenAI Chat Completions and Claude Messages -- payload shape,
response parsing, turn assembly, endpoint layout, and stream framing -- lives
here, so a protocol quirk is fixed once instead of in every branch of the call
path.

A protocol never performs I/O.  ``parse_stream`` consumes an iterator of
already-framed SSE packets, which keeps HTTP and SSE concerns in the gateway.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from typing import Any

from app.domain.models import ModelAlias
from app.domain.tooling import ToolSpec


@dataclass
class RoundResult:
    """One model turn: visible text plus any provider-native tool calls.

    ``assistant_message`` is the provider's own representation of the turn.  It
    must be echoed back verbatim in the next request, because both providers
    require the assistant tool-call turn to precede its tool results.
    """

    text: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_message: Any = None


# A round runner yields public stream events and returns the completed turn.
RoundRunner = Generator[dict[str, Any], None, RoundResult]


def _rewrite_path(base_url: str, rewrite: Callable[[str], str]) -> str:
    parts = urllib.parse.urlsplit(base_url.rstrip("/"))
    path = rewrite(parts.path.rstrip("/"))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _strip_completion_suffix(path: str) -> str:
    for suffix in ("/chat/completions", "/messages"):
        if path.endswith(suffix):
            return path[: -len(suffix)]
    return path


class ModelProtocol:
    """One provider wire protocol.

    Subclasses translate between the gateway's neutral message list and a
    specific provider dialect.  They hold no state and perform no I/O.
    """

    name = ""

    # -- request construction ------------------------------------------------

    def tool_schema(self, tool: ToolSpec) -> dict[str, Any]:
        raise NotImplementedError

    def payload(
        self,
        model: ModelAlias,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        payload = self._base_payload(model, messages, max_tokens=max_tokens)
        if model.temperature is not None:
            payload["temperature"] = model.temperature
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream"] = True
        return payload

    def _base_payload(
        self,
        model: ModelAlias,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def endpoint(self, base_url: str) -> str:
        raise NotImplementedError

    def models_endpoint(self, base_url: str) -> str:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        """Protocol-mandated headers; authentication is added by the gateway."""

        return {}

    # -- response parsing ----------------------------------------------------

    def text(self, raw: dict[str, Any]) -> str:
        """Extract assistant text from a non-tool completion."""

        raise NotImplementedError

    def parse_response(self, raw: dict[str, Any]) -> RoundResult:
        """Extract text, tool calls, and the echo-back assistant turn."""

        raise NotImplementedError

    def parse_stream(self, packets: Iterator[dict[str, Any]]) -> RoundRunner:
        """Turn framed SSE packets into public events plus one ``RoundResult``."""

        raise NotImplementedError

    # -- transcript assembly -------------------------------------------------

    def assistant_turn(self, result: RoundResult) -> dict[str, Any]:
        """The assistant tool-call turn to append before its tool results."""

        raise NotImplementedError

    def tool_result_turns(self, results: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        """Messages carrying ``(call_id, result)`` pairs back to the model."""

        raise NotImplementedError

    def elide(self, prefix: list[dict[str, Any]], notice: str) -> list[dict[str, Any]]:
        """Attach the transcript-elision notice to the preserved prefix."""

        raise NotImplementedError


class OpenAIProtocol(ModelProtocol):
    """OpenAI Chat Completions: ``tool_calls`` plus ``role: tool`` results."""

    name = "openai"

    def tool_schema(self, tool: ToolSpec) -> dict[str, Any]:
        return tool.for_openai()

    def _base_payload(
        self,
        model: ModelAlias,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model.model, "messages": messages}
        selected = max_tokens if max_tokens is not None else model.max_tokens
        if selected is not None:
            payload["max_tokens"] = selected
        return payload

    def endpoint(self, base_url: str) -> str:
        return _rewrite_path(
            base_url,
            lambda path: path if path.endswith("/chat/completions") else path + "/chat/completions",
        )

    def models_endpoint(self, base_url: str) -> str:
        def rewrite(path: str) -> str:
            path = _strip_completion_suffix(path)
            return path if path.endswith("/models") else path + "/models"

        return _rewrite_path(base_url, rewrite)

    def text(self, raw: dict[str, Any]) -> str:
        try:
            content = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("OpenAI 响应缺少 choices[0].message.content") from error
        content = _join_text_parts(content)
        if not isinstance(content, str):
            raise ValueError("模型响应内容必须是字符串")
        return content

    def parse_response(self, raw: dict[str, Any]) -> RoundResult:
        try:
            message = raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("OpenAI 响应缺少 choices[0].message") from error
        if not isinstance(message, dict):
            raise ValueError("OpenAI message must be an object")
        calls: list[dict[str, Any]] = []
        for item in message.get("tool_calls", []) or []:
            if not isinstance(item, dict):
                continue
            function = item.get("function", {})
            if not isinstance(function, dict):
                continue
            calls.append({
                "id": str(item.get("id", "")),
                "name": str(function.get("name", "")),
                "arguments": _decode_arguments(function.get("arguments", {})),
            })
        content = _join_text_parts(message.get("content", ""))
        return RoundResult(str(content or ""), calls, message)

    def assistant_turn(self, result: RoundResult) -> dict[str, Any]:
        message = result.assistant_message
        if isinstance(message, dict):
            return message
        return {"role": "assistant", "content": result.text}

    def tool_result_turns(self, results: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        return [
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False),
            }
            for call_id, result in results
        ]

    def elide(self, prefix: list[dict[str, Any]], notice: str) -> list[dict[str, Any]]:
        return [*prefix, {"role": "system", "content": notice.strip()}]

    def parse_stream(self, packets: Iterator[dict[str, Any]]) -> RoundRunner:
        text_parts: list[str] = []
        calls_by_index: dict[int, dict[str, Any]] = {}
        for packet in packets:
            data = packet.get("data")
            if not isinstance(data, dict):
                continue
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            if packet.get("event") == "json":
                # A few local OpenAI-compatible servers ignore ``stream`` and
                # return one ordinary completion.  Treat it as a single delta so
                # the SSE API still completes rather than producing empty text.
                fallback = self.parse_response(data)
                if fallback.text:
                    text_parts.append(fallback.text)
                    yield {"type": "text_delta", "text": fallback.text}
                message = fallback.assistant_message
                raw_calls = message.get("tool_calls", []) if isinstance(message, dict) else []
                for index, call in enumerate(fallback.calls):
                    raw_call = raw_calls[index] if index < len(raw_calls) else {}
                    function = raw_call.get("function", {}) if isinstance(raw_call, dict) else {}
                    raw_arguments = function.get("arguments", "") if isinstance(function, dict) else ""
                    if not isinstance(raw_arguments, str):
                        raw_arguments = json.dumps(raw_arguments, ensure_ascii=False)
                    calls_by_index[index] = {
                        "id": str(call.get("id", "")),
                        "name": str(call.get("name", "")),
                        "arguments_raw": raw_arguments,
                    }
                continue
            choices = data.get("choices", [])
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    continue
                content = _join_text_parts(delta.get("content", ""))
                if content:
                    text_parts.append(str(content))
                    yield {"type": "text_delta", "text": str(content)}
                raw_calls = delta.get("tool_calls", []) or []
                if not isinstance(raw_calls, list):
                    continue
                for fallback_index, item in enumerate(raw_calls):
                    if not isinstance(item, dict):
                        continue
                    try:
                        index = int(item.get("index", fallback_index))
                    except (TypeError, ValueError):
                        index = fallback_index
                    current = calls_by_index.setdefault(
                        index, {"id": "", "name": "", "arguments_raw": ""}
                    )
                    if item.get("id"):
                        current["id"] = str(item["id"])
                    function = item.get("function", {})
                    if not isinstance(function, dict):
                        continue
                    if function.get("name"):
                        current["name"] = str(function["name"])
                    if function.get("arguments"):
                        current["arguments_raw"] += str(function["arguments"])

        calls: list[dict[str, Any]] = []
        wire_calls: list[dict[str, Any]] = []
        for index in sorted(calls_by_index):
            current = calls_by_index[index]
            raw_arguments = str(current.get("arguments_raw", ""))
            calls.append({
                "id": str(current.get("id", "")),
                "name": str(current.get("name", "")),
                "arguments": _decode_arguments(raw_arguments or "{}"),
            })
            wire_calls.append({
                "id": str(current.get("id", "")),
                "type": "function",
                "function": {
                    "name": str(current.get("name", "")),
                    "arguments": raw_arguments,
                },
            })
        text = "".join(text_parts)
        assistant_message: dict[str, Any] = {"role": "assistant", "content": text}
        if wire_calls:
            assistant_message["tool_calls"] = wire_calls
            if not text:
                assistant_message["content"] = None
        return RoundResult(text, calls, assistant_message)


class ClaudeProtocol(ModelProtocol):
    """Claude Messages: ``tool_use`` blocks answered by one batched user turn."""

    name = "claude"

    def tool_schema(self, tool: ToolSpec) -> dict[str, Any]:
        return tool.for_claude()

    def _base_payload(
        self,
        model: ModelAlias,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        return {
            "model": model.model,
            "system": "\n\n".join(
                str(item["content"]) for item in messages if item["role"] == "system"
            ),
            "messages": [item for item in messages if item["role"] != "system"],
            # Claude requires max_tokens.  Callers that know the compaction
            # reserve pass it here so the output allowance matches the budget
            # that shaped the input; 4096 is the last resort for callers with no
            # budget context.
            "max_tokens": max_tokens or model.max_tokens or 4096,
        }

    def endpoint(self, base_url: str) -> str:
        def rewrite(path: str) -> str:
            if path.endswith("/messages"):
                return path
            return path + ("/messages" if path.endswith("/v1") else "/v1/messages")

        return _rewrite_path(base_url, rewrite)

    def models_endpoint(self, base_url: str) -> str:
        def rewrite(path: str) -> str:
            path = _strip_completion_suffix(path)
            if path.endswith("/models"):
                return path
            return path + ("/models" if path.endswith("/v1") else "/v1/models")

        return _rewrite_path(base_url, rewrite)

    def headers(self) -> dict[str, str]:
        return {"anthropic-version": "2023-06-01"}

    def text(self, raw: dict[str, Any]) -> str:
        return "".join(
            str(item.get("text", ""))
            for item in self._blocks(raw)
            if isinstance(item, dict) and item.get("type") == "text"
        )

    def parse_response(self, raw: dict[str, Any]) -> RoundResult:
        blocks = self._blocks(raw)
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                calls.append({
                    "id": str(block.get("id", "")),
                    "name": str(block.get("name", "")),
                    "arguments": block.get("input", {}),
                })
        return RoundResult("".join(text_parts), calls, blocks)

    @staticmethod
    def _blocks(raw: dict[str, Any]) -> list[Any]:
        blocks = raw.get("content")
        if not isinstance(blocks, list):
            raise ValueError("Claude 响应缺少 content 数组")
        return blocks

    def assistant_turn(self, result: RoundResult) -> dict[str, Any]:
        blocks = result.assistant_message
        return {"role": "assistant", "content": blocks if isinstance(blocks, list) else []}

    def tool_result_turns(self, results: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        # Claude requires every tool result for one assistant turn to arrive in a
        # single user turn; separate turns would be consecutive same-role turns.
        if not results:
            return []
        return [{
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
                for call_id, result in results
            ],
        }]

    def elide(self, prefix: list[dict[str, Any]], notice: str) -> list[dict[str, Any]]:
        # Claude rejects two consecutive user turns, so the notice is merged into
        # the trailing prefix message instead of inserted as its own turn.
        head = [dict(item) for item in prefix]
        for item in reversed(head):
            if item.get("role") == "user":
                item["content"] = str(item.get("content", "")) + notice
                break
        return head

    def parse_stream(self, packets: Iterator[dict[str, Any]]) -> RoundRunner:
        blocks: dict[int, dict[str, Any]] = {}
        current_index = 0
        for packet in packets:
            data = packet.get("data")
            if not isinstance(data, dict):
                continue
            event_name = str(packet.get("event") or data.get("type") or "")
            if event_name == "error" or data.get("error"):
                raise RuntimeError(str(data.get("error", data)))
            if packet.get("event") == "json":
                fallback = self.parse_response(data)
                if fallback.text:
                    yield {"type": "text_delta", "text": fallback.text}
                blocks = {}
                for index, block in enumerate(fallback.assistant_message or []):
                    if not isinstance(block, dict):
                        continue
                    blocks[index] = {
                        "type": str(block.get("type", "text")),
                        "text": str(block.get("text", "")),
                        "id": str(block.get("id", "")),
                        "name": str(block.get("name", "")),
                        "input_json": json.dumps(block.get("input", {}), ensure_ascii=False),
                    }
                if not blocks and fallback.text:
                    blocks = {0: {
                        "type": "text", "text": fallback.text,
                        "id": "", "name": "", "input_json": "",
                    }}
                continue
            if event_name == "content_block_start":
                try:
                    index = int(data.get("index", len(blocks)))
                except (TypeError, ValueError):
                    index = len(blocks)
                current_index = index
                block = data.get("content_block", {})
                if not isinstance(block, dict):
                    block = {}
                blocks[index] = {
                    "type": str(block.get("type", "text")),
                    "text": str(block.get("text", "")),
                    "id": str(block.get("id", "")),
                    "name": str(block.get("name", "")),
                    "input_json": "",
                }
                if blocks[index]["text"]:
                    yield {"type": "text_delta", "text": blocks[index]["text"]}
                continue
            if event_name != "content_block_delta":
                continue
            try:
                index = int(data.get("index", current_index))
            except (TypeError, ValueError):
                index = current_index
            current = blocks.setdefault(
                index, {"type": "text", "text": "", "id": "", "name": "", "input_json": ""}
            )
            delta = data.get("delta", {})
            if not isinstance(delta, dict):
                continue
            if delta.get("type") == "text_delta":
                text = str(delta.get("text", ""))
                current["text"] += text
                if text:
                    yield {"type": "text_delta", "text": text}
            elif delta.get("type") == "input_json_delta":
                current["input_json"] += str(delta.get("partial_json", ""))

        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        assistant_blocks: list[dict[str, Any]] = []
        for index in sorted(blocks):
            block = blocks[index]
            if block.get("type") == "tool_use":
                arguments = _decode_arguments(str(block.get("input_json", "")) or "{}")
                calls.append({
                    "id": str(block.get("id", "")),
                    "name": str(block.get("name", "")),
                    "arguments": arguments,
                })
                assistant_blocks.append({
                    "type": "tool_use",
                    "id": str(block.get("id", "")),
                    "name": str(block.get("name", "")),
                    "input": arguments if isinstance(arguments, dict) else {},
                })
            else:
                text = str(block.get("text", ""))
                if text:
                    text_parts.append(text)
                    assistant_blocks.append({"type": "text", "text": text})
        return RoundResult("".join(text_parts), calls, assistant_blocks)


_PROTOCOLS: dict[str, ModelProtocol] = {
    OpenAIProtocol.name: OpenAIProtocol(),
    ClaudeProtocol.name: ClaudeProtocol(),
}


def protocol_for(name: str) -> ModelProtocol:
    """Resolve a protocol adapter, defaulting to OpenAI-compatible.

    The default preserves the gateway's long-standing behaviour: anything that
    is not explicitly Claude is treated as OpenAI-compatible, which is what
    local gateways rely on.
    """

    return _PROTOCOLS.get(name, _PROTOCOLS[OpenAIProtocol.name])


def _join_text_parts(content: Any) -> Any:
    """Flatten a provider content list into text, leaving scalars untouched."""

    if isinstance(content, list):
        return "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type", "text") == "text"
        )
    return content


def _decode_arguments(raw: Any) -> Any:
    """Decode tool arguments, returning ``None`` for malformed JSON.

    ``None`` is deliberate: the tool loop rejects non-dict arguments and feeds
    that error back to the model, which is more useful than crashing the round.
    """

    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
