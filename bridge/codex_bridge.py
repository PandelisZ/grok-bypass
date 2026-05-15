#!/usr/bin/env python3
"""Minimal local OpenAI-compatible bridge backed by `codex exec`.

This intentionally shells out to the official Codex CLI instead of reading or
replaying any Codex auth tokens. It is slow compared with a real model API
because every request starts a fresh non-interactive Codex run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


DEFAULT_MODEL = os.environ.get("CODEX_BRIDGE_MODEL", "codex-gpt-5.5")
DEFAULT_CODEX_MODEL = os.environ.get("CODEX_BRIDGE_CODEX_MODEL", "gpt-5.5")
DEFAULT_CODEX_CMD = os.environ.get("CODEX_BRIDGE_CODEX_CMD", "codex")
MODEL_ALIASES = {DEFAULT_MODEL, "grok-build"}


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


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
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(part for part in parts if part)
    return str(content)


def prompt_from_responses(body: dict[str, Any]) -> str:
    value = body.get("input", "")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, dict):
                role = item.get("role", "user")
                content = normalize_content(item.get("content"))
                if content:
                    chunks.append(f"{role}: {content}")
            else:
                chunks.append(str(item))
        return "\n\n".join(chunks)
    return str(value)


def prompt_from_chat(body: dict[str, Any]) -> str:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return str(messages)
    chunks: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            chunks.append(str(message))
            continue
        role = message.get("role", "user")
        content = normalize_content(message.get("content"))
        if content:
            chunks.append(f"{role}: {content}")
    return "\n\n".join(chunks)


def run_codex(prompt: str, model: str, cwd: str | None) -> str:
    command = [
        DEFAULT_CODEX_CMD,
        "exec",
        "-m",
        model,
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-rules",
        "--color",
        "never",
        "--json",
        "--sandbox",
        "read-only",
        prompt,
    ]
    proc = subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=int(os.environ.get("CODEX_BRIDGE_TIMEOUT", "240")),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "codex exec failed")

    last_message = ""
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            last_message = item["text"]
    if not last_message:
        raise RuntimeError("codex exec completed without an agent_message")
    return last_message


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "codex-bridge/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_sse(self, events: list[dict[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event in events:
            if isinstance(event.get("type"), str):
                self.wfile.write(f"event: {event['type']}\n".encode("utf-8"))
            self.wfile.write(b"data: " + json_bytes(event) + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def send_error_json(self, message: str, status: int = 500) -> None:
        self.send_json({"error": {"message": message, "type": "bridge_error"}}, status=status)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/models":
            self.send_json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": DEFAULT_MODEL,
                            "object": "model",
                            "created": 0,
                            "owned_by": "local-codex-bridge",
                        }
                    ],
                }
            )
            return
        if self.path.rstrip("/") == "/health":
            self.send_json({"ok": True, "model": DEFAULT_MODEL, "codex_model": DEFAULT_CODEX_MODEL})
            return
        self.send_error_json("not found", status=404)

    def do_POST(self) -> None:
        try:
            if self.path.rstrip("/") == "/v1/responses":
                self.handle_responses()
                return
            if self.path.rstrip("/") == "/v1/chat/completions":
                self.handle_chat_completions()
                return
            self.send_error_json("not found", status=404)
        except subprocess.TimeoutExpired:
            self.send_error_json("codex exec timed out", status=504)
        except Exception as exc:
            self.send_error_json(str(exc), status=500)

    def handle_responses(self) -> None:
        body = self.read_json()
        requested_model = str(body.get("model") or DEFAULT_MODEL)
        codex_model = DEFAULT_CODEX_MODEL if requested_model in MODEL_ALIASES else requested_model
        prompt = prompt_from_responses(body)
        text = run_codex(prompt, codex_model, os.getcwd())
        response_id = "resp_" + uuid.uuid4().hex
        message_id = "msg_" + uuid.uuid4().hex
        created = int(time.time())
        output_item = {
            "id": message_id,
            "type": "message",
            "status": "completed",
            "content": [{"type": "output_text", "annotations": [], "logprobs": [], "text": text}],
            "phase": "final_answer",
            "role": "assistant",
        }
        base_response = {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "background": False,
            "completed_at": created,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "max_output_tokens": body.get("max_output_tokens"),
            "model": requested_model,
            "output": [output_item],
            "parallel_tool_calls": True,
            "previous_response_id": None,
            "reasoning": {"effort": body.get("reasoning", {}).get("effort") if isinstance(body.get("reasoning"), dict) else None, "summary": None},
            "store": False,
            "text": {"format": {"type": "text"}, "verbosity": "medium"},
            "tool_choice": "auto",
            "tools": [],
            "truncation": "disabled",
            "usage": None,
            "metadata": {},
        }
        if body.get("stream"):
            self.send_sse(
                [
                    {
                        "type": "response.created",
                        "response": {**base_response, "status": "in_progress", "completed_at": None, "output": []},
                        "sequence_number": 0,
                    },
                    {
                        "type": "response.output_item.added",
                        "item": {**output_item, "status": "in_progress", "content": []},
                        "output_index": 0,
                        "sequence_number": 1,
                    },
                    {
                        "type": "response.content_part.added",
                        "content_index": 0,
                        "item_id": message_id,
                        "output_index": 0,
                        "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": ""},
                        "sequence_number": 2,
                    },
                    {
                        "type": "response.output_text.delta",
                        "item_id": message_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": text,
                        "logprobs": [],
                        "sequence_number": 3,
                    },
                    {
                        "type": "response.output_text.done",
                        "item_id": message_id,
                        "output_index": 0,
                        "content_index": 0,
                        "text": text,
                        "logprobs": [],
                        "sequence_number": 4,
                    },
                    {
                        "type": "response.content_part.done",
                        "content_index": 0,
                        "item_id": message_id,
                        "output_index": 0,
                        "part": output_item["content"][0],
                        "sequence_number": 5,
                    },
                    {
                        "type": "response.output_item.done",
                        "item": output_item,
                        "output_index": 0,
                        "sequence_number": 6,
                    },
                    {
                        "type": "response.completed",
                        "response": base_response,
                        "sequence_number": 7,
                    },
                ]
            )
            return
        self.send_json({**base_response, "output_text": text})

    def handle_chat_completions(self) -> None:
        body = self.read_json()
        requested_model = str(body.get("model") or DEFAULT_MODEL)
        codex_model = DEFAULT_CODEX_MODEL if requested_model in MODEL_ALIASES else requested_model
        prompt = prompt_from_chat(body)
        text = run_codex(prompt, codex_model, os.getcwd())
        completion_id = "chatcmpl_" + uuid.uuid4().hex
        created = int(time.time())
        if body.get("stream"):
            self.send_sse(
                [
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": requested_model,
                        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    },
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": requested_model,
                        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                    },
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": requested_model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ]
            )
            return
        self.send_json(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": requested_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI-compatible bridge backed by codex exec")
    parser.add_argument("--host", default=os.environ.get("CODEX_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CODEX_BRIDGE_PORT", "11435")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    print(f"codex bridge listening on http://{args.host}:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
