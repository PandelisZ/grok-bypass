"""Request and response shaping for the Codex Responses endpoint."""

from __future__ import annotations

import json
from typing import Any, BinaryIO, Callable

from codex_auth import BridgeError


def normalize_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(part for part in parts if part)
    return str(content)


def responses_input_from_chat(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return [{"role": "user", "content": str(messages)}]

    items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            items.append({"role": "user", "content": str(message)})
            continue
        role = str(message.get("role") or "user")
        if role not in {"user", "assistant", "system", "developer"}:
            role = "user"
        content = normalize_content(message.get("content"))
        if content:
            items.append({"role": role, "content": content})
    return items


def codex_content(content: Any, input_kind: str = "input_text") -> list[dict[str, Any]]:
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("type"), str):
                item_type = item["type"]
                if item_type in {"input_text", "output_text"}:
                    parts.append(dict(item, type=input_kind))
                else:
                    parts.append(dict(item))
            else:
                text = normalize_content(item)
                if text:
                    parts.append({"type": input_kind, "text": text})
        return parts

    text = normalize_content(content)
    return [{"type": input_kind, "text": text}] if text else []


def collect_instruction_parts(input_value: Any) -> list[str]:
    if not isinstance(input_value, list):
        return []
    parts: list[str] = []
    for item in input_value:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {None, "message"}:
            continue
        role = str(item.get("role") or "")
        if role in {"system", "developer"}:
            content = normalize_content(item.get("content"))
            if content:
                parts.append(content)
    return parts


def codex_input_items(input_value: Any) -> list[dict[str, Any]]:
    if isinstance(input_value, str):
        return [{"type": "message", "role": "user", "content": codex_content(input_value)}]
    if not isinstance(input_value, list):
        return [{"type": "message", "role": "user", "content": codex_content(str(input_value))}]

    items: list[dict[str, Any]] = []
    for item in input_value:
        if not isinstance(item, dict):
            items.append({"type": "message", "role": "user", "content": codex_content(str(item))})
            continue

        item_type = item.get("type")
        if item_type and item_type != "message":
            items.append(dict(item))
            continue

        role = str(item.get("role") or "user")
        if role in {"system", "developer"}:
            continue
        content_kind = "output_text" if role == "assistant" else "input_text"
        items.append(
            {
                "type": "message",
                "role": role,
                "content": codex_content(item.get("content"), content_kind),
            }
        )
    return items


def codex_wire_body(
    body: dict[str, Any],
    default_model: str,
    upstream_model: Callable[[str], str],
) -> dict[str, Any]:
    instructions = body.get("instructions") or "You are ChatGPT, a large language model trained by OpenAI."
    instruction_parts = collect_instruction_parts(body.get("input", ""))
    if instruction_parts:
        instructions = "\n\n".join([str(instructions), *instruction_parts])

    request_body: dict[str, Any] = {
        "model": upstream_model(str(body.get("model") or default_model)),
        "instructions": instructions,
        "input": codex_input_items(body.get("input", "")),
        "tools": body.get("tools") if isinstance(body.get("tools"), list) else [],
        "tool_choice": body.get("tool_choice") or "auto",
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "store": bool(body.get("store", False)),
        "stream": bool(body.get("stream", False)),
        "include": body.get("include") if isinstance(body.get("include"), list) else [],
        "client_metadata": body.get("client_metadata")
        if isinstance(body.get("client_metadata"), dict)
        else {"x-codex-installation-id": "grok-codex-bridge"},
    }
    for key in ("reasoning", "service_tier", "prompt_cache_key", "text"):
        if key in body:
            request_body[key] = body[key]
    return request_body


def extract_output_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]

    chunks: list[str] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    return "".join(chunks)


def response_from_sse(response: BinaryIO) -> dict[str, Any]:
    completed: dict[str, Any] | None = None
    text_parts: list[str] = []
    current_event_lines: list[str] = []

    def handle_event(lines: list[str]) -> None:
        nonlocal completed
        data_lines = [line[5:].lstrip() for line in lines if line.startswith("data:")]
        if not data_lines:
            return
        data = "\n".join(data_lines)
        if data == "[DONE]":
            return
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
            text_parts.append(event["delta"])
        elif event_type == "response.output_text.done" and isinstance(event.get("text"), str):
            text_parts.clear()
            text_parts.append(event["text"])
        elif event_type in {"response.completed", "response.failed"} and isinstance(
            event.get("response"), dict
        ):
            completed = event["response"]

    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            handle_event(current_event_lines)
            current_event_lines = []
        else:
            current_event_lines.append(line)
    if current_event_lines:
        handle_event(current_event_lines)

    if completed is None:
        raise BridgeError("Codex backend stream ended without response.completed", 502)
    completed.setdefault("output_text", "".join(text_parts) or extract_output_text(completed))
    return completed
