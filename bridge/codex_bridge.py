#!/usr/bin/env python3
"""Minimal local OpenAI-compatible bridge backed by Codex ChatGPT auth.

The bridge exposes a tiny localhost `/v1` surface for tools that can speak the
Responses API. It forwards model requests to the same Codex backend used by the
Codex CLI when logged in with ChatGPT: `https://chatgpt.com/backend-api/codex`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO

from codex_auth import BridgeError, CodexAuthStore, json_bytes
from codex_wire import codex_wire_body
from codex_wire import extract_output_text
from codex_wire import response_from_sse
from codex_wire import responses_input_from_chat


DEFAULT_MODEL = os.environ.get("CODEX_BRIDGE_MODEL", "codex-gpt-5.5")
DEFAULT_UPSTREAM_MODEL = os.environ.get("CODEX_BRIDGE_UPSTREAM_MODEL", "gpt-5.5")
DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
DEFAULT_CODEX_BASE_URL = os.environ.get(
    "CODEX_BRIDGE_CODEX_BASE_URL",
    "https://chatgpt.com/backend-api/codex",
).rstrip("/")
MODEL_ALIASES = {DEFAULT_MODEL, "grok-build"}


def upstream_model(model: str) -> str:
    return DEFAULT_UPSTREAM_MODEL if model in MODEL_ALIASES else model


def write_sse_event(wfile: Any, event: dict[str, Any]) -> None:
    if isinstance(event.get("type"), str):
        wfile.write(f"event: {event['type']}\n".encode("utf-8"))
    wfile.write(b"data: " + json_bytes(event) + b"\n\n")
    wfile.flush()


def relay_responses_stream(response: BinaryIO, wfile: Any) -> None:
    output_items: list[dict[str, Any]] = []
    current_event_lines: list[str] = []

    def handle_event(lines: list[str]) -> None:
        data_lines = [line[5:].lstrip() for line in lines if line.startswith("data:")]
        if not data_lines:
            return
        data = "\n".join(data_lines)
        if data == "[DONE]":
            wfile.write(b"data: [DONE]\n\n")
            wfile.flush()
            return
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            wfile.write(("data: " + data + "\n\n").encode("utf-8"))
            wfile.flush()
            return
        if not isinstance(event, dict):
            return

        if event.get("type") == "response.output_item.done" and isinstance(event.get("item"), dict):
            output_items.append(event["item"])
        elif event.get("type") in {"response.completed", "response.incomplete"}:
            response_obj = event.get("response")
            if isinstance(response_obj, dict) and not response_obj.get("output"):
                response_obj["output"] = output_items
                event["response"] = response_obj
        write_sse_event(wfile, event)

    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            handle_event(current_event_lines)
            current_event_lines = []
        else:
            current_event_lines.append(line)
    if current_event_lines:
        handle_event(current_event_lines)


class CodexResponsesClient:
    def __init__(self, auth_store: CodexAuthStore, base_url: str) -> None:
        self.auth_store = auth_store
        self.responses_url = f"{base_url}/responses"

    def open_response(self, body: dict[str, Any], stream: bool) -> BinaryIO:
        request_body = codex_wire_body(body, DEFAULT_MODEL, upstream_model)
        request_body["stream"] = stream
        return self._open_response(request_body, stream, allow_refresh=True)

    def _open_response(
        self,
        body: dict[str, Any],
        stream: bool,
        allow_refresh: bool,
    ) -> BinaryIO:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            **self.auth_store.auth_headers(),
        }
        request = urllib.request.Request(
            self.responses_url,
            data=json_bytes(body),
            headers=headers,
            method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=300)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and allow_refresh:
                self.auth_store.refresh_after_unauthorized()
                return self._open_response(body, stream, allow_refresh=False)
            detail = exc.read().decode("utf-8", errors="replace")
            raise BridgeError(detail or f"Codex backend returned HTTP {exc.code}", exc.code) from exc
        except Exception as exc:
            raise BridgeError(f"Codex backend request failed: {exc}", 502) from exc

    def response_json(self, body: dict[str, Any]) -> dict[str, Any]:
        # The Codex backend requires streaming requests. For non-stream callers,
        # consume the SSE stream and reconstruct an ordinary Responses payload.
        with self.open_response(body, stream=True) as response:
            return response_from_sse(response)


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "codex-oauth-bridge/0.2"

    @property
    def codex(self) -> CodexResponsesClient:
        return self.server.codex  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise BridgeError("request body must be a JSON object", 400)
        return value

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 500) -> None:
        self.send_json({"error": {"message": message, "type": "bridge_error"}}, status=status)

    def send_sse_events(self, events: list[dict[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event in events:
            self.wfile.write(b"data: " + json_bytes(event) + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

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
                            "owned_by": "codex-oauth-bridge",
                        },
                        {
                            "id": DEFAULT_UPSTREAM_MODEL,
                            "object": "model",
                            "created": 0,
                            "owned_by": "codex-oauth-bridge",
                        },
                    ],
                }
            )
            return
        if self.path.rstrip("/") == "/health":
            self.send_json({"ok": True, "model": DEFAULT_MODEL, "upstream_model": DEFAULT_UPSTREAM_MODEL})
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
        except BridgeError as exc:
            sys.stderr.write(f"bridge error {exc.status}: {str(exc)[:500]}\n")
            self.send_error_json(str(exc), status=exc.status)
        except Exception as exc:
            self.send_error_json(str(exc), status=500)

    def handle_responses(self) -> None:
        body = self.read_json()
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with self.codex.open_response(body, stream=True) as response:
                relay_responses_stream(response, self.wfile)
            return

        response = self.codex.response_json(body)
        response.setdefault("output_text", extract_output_text(response))
        self.send_json(response)

    def handle_chat_completions(self) -> None:
        body = self.read_json()
        requested_model = str(body.get("model") or DEFAULT_MODEL)
        responses_body: dict[str, Any] = {
            "model": requested_model,
            "input": responses_input_from_chat(body),
        }
        for source, target in [
            ("max_tokens", "max_output_tokens"),
            ("max_completion_tokens", "max_output_tokens"),
            ("temperature", "temperature"),
            ("top_p", "top_p"),
        ]:
            if source in body:
                responses_body[target] = body[source]

        response = self.codex.response_json(responses_body)
        text = extract_output_text(response)
        completion_id = "chatcmpl_" + uuid.uuid4().hex
        created = int(time.time())
        if body.get("stream"):
            self.send_sse_events(
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
                "usage": response.get("usage"),
            }
        )


class CodexBridgeServer(ThreadingHTTPServer):
    codex: CodexResponsesClient


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI-compatible bridge backed by Codex OAuth Responses API")
    parser.add_argument("--host", default=os.environ.get("CODEX_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CODEX_BRIDGE_PORT", "11435")))
    parser.add_argument("--codex-home", default=str(DEFAULT_CODEX_HOME))
    parser.add_argument("--codex-base-url", default=DEFAULT_CODEX_BASE_URL)
    args = parser.parse_args()

    auth_store = CodexAuthStore(Path(args.codex_home).expanduser())
    server = CodexBridgeServer((args.host, args.port), BridgeHandler)
    server.codex = CodexResponsesClient(auth_store, args.codex_base_url.rstrip("/"))
    print(f"codex oauth bridge listening on http://{args.host}:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
