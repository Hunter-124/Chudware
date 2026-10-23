"""Local official-wire SSE fixtures, not a provider, model or billing attestation.

Only synthetic keys are accepted. Real cells, the real gateway broker and the
pinned native SDK consume these responses; no RPC/daemon/auth path is replaced.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import socket
import threading
import time


TEXT = "LOCAL-WIRE-READY\n"
THINKING = "Local fixture thinking; preserve these exact bytes."
SIGNATURE = "test-only-signature-not-a-provider-attestation-7fd0"


def sse(kind, data):
    return ("event: " + kind + "\ndata: " + json.dumps(data, separators=(",", ":")) + "\n\n").encode()


@dataclass
class WireRequest:
    path: str
    key: str
    body: dict
    raw: bytes
    hold: threading.Event = field(default_factory=threading.Event)
    first_delta: threading.Event = field(default_factory=threading.Event)
    closed: threading.Event = field(default_factory=threading.Event)
    completed: bool = False
    tool_call: tuple[str, str] | None = None


class OfficialWireFixture:
    """Own one loopback ASGI server with bounded request records and held SSE."""
    def __init__(self, keys):
        self.keys = set(keys)
        self.records = []
        self.condition = threading.Condition()
        self.held_keys = set()
        self.tool_calls = {}
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.server = None
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        with self.condition:
            if not self.condition.wait_for(lambda: self.server is not None or self.error is not None, 15):
                raise RuntimeError("local official-wire fixture failed to start")
        if self.error is not None:
            raise RuntimeError("local official-wire fixture startup failed") from self.error
        deadline = time.monotonic() + 15
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.close()
                raise RuntimeError("local official-wire fixture did not become ready")
            time.sleep(0.01)

    def _run(self):
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            with self.condition:
                self.error = exc
                self.condition.notify_all()

    async def _serve(self):
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, StreamingResponse
        import uvicorn
        app = FastAPI()
        # FastAPI resolves annotations against globals, not this local import.
        globals()["Request"] = Request

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def wire(request: Request, path: str):
            key = request.headers.get("x-api-key") or request.headers.get("x-goog-api-key") or ""
            if not key:
                auth = request.headers.get("authorization", "")
                key = auth[7:] if auth.startswith("Bearer ") else ""
            if key not in self.keys:
                return JSONResponse({"error": {"message": "fixture synthetic key required"}}, status_code=401)
            if request.method == "GET" and path.endswith("/models"):
                if request.headers.get("anthropic-version"):
                    model = {"id": "claude-sonnet-4-5", "type": "model", "display_name": "Local wire fixture",
                             "created_at": "2025-01-01T00:00:00Z"}
                    return JSONResponse({"data": [model], "has_more": False,
                                         "first_id": model["id"], "last_id": model["id"]})
                model = {"id": "gpt-4o-mini", "object": "model", "created": 1, "owned_by": "local-fixture"}
                return JSONResponse({"object": "list", "data": [model]})
            if request.method != "POST" or not path.endswith(("/messages", "/responses")):
                return JSONResponse({"error": {"message": "fixture route unavailable"}}, status_code=404)
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 8 * 1024 * 1024:
                    return JSONResponse({"error": {"message": "fixture body bound"}}, status_code=413)
            body = json.loads(raw)
            row = WireRequest("/" + path, key, body, bytes(raw))
            with self.condition:
                if len(self.records) >= 128:
                    return JSONResponse({"error": {"message": "fixture record bound"}}, status_code=429)
                if (path.endswith("/responses") and key in self.tool_calls
                        and any(tool.get("name") == "bash" for tool in body.get("tools", []))):
                    name, arguments = self.tool_calls[key]
                    if not any(tool.get("type") == "function" and tool.get("name") == name
                               for tool in body.get("tools", [])):
                        return JSONResponse({"error": {"message": "scripted fixture tool was not advertised"}}, status_code=400)
                    row.tool_call = self.tool_calls.pop(key)
                if key not in self.held_keys:
                    row.hold.set()
                self.records.append(row)
                self.condition.notify_all()
            stream = self._anthropic(row) if path.endswith("/messages") else self._openai(row)
            return StreamingResponse(stream, media_type="text/event-stream", headers={"Cache-Control": "no-store"})

        class FixtureServer(uvicorn.Server):
            @contextmanager
            def capture_signals(self):
                yield
        server = FixtureServer(uvicorn.Config(app, log_level="critical", access_log=False,
                                             lifespan="off", timeout_graceful_shutdown=2))
        with self.condition:
            self.server = server
            self.condition.notify_all()
        await server.serve(sockets=[self.listener])

    async def _pause(self, row):
        row.first_delta.set()
        while not row.hold.is_set():
            await asyncio.sleep(0.02)

    async def _anthropic(self, row):
        try:
            message = {"id": "msg_local_fixture", "type": "message", "role": "assistant", "model": row.body["model"],
                       "content": [], "stop_reason": None, "stop_sequence": None,
                       "usage": {"input_tokens": 11, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
            yield sse("message_start", {"type": "message_start", "message": message})
            yield sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
            yield sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": THINKING}})
            yield sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": SIGNATURE}})
            yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            yield sse("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
            yield sse("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": TEXT}})
            await self._pause(row)
            yield sse("content_block_stop", {"type": "content_block_stop", "index": 1})
            yield sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 17}})
            yield sse("message_stop", {"type": "message_stop"})
            row.completed = True
        finally:
            row.closed.set()

    async def _openai(self, row):
        try:
            if row.tool_call is not None:
                name, arguments = row.tool_call
                response = {"id": "resp_local_tool_fixture", "object": "response", "created_at": 1,
                            "status": "in_progress", "model": row.body["model"], "output": [],
                            "error": None, "incomplete_details": None}
                item = {"id": "fc_local_fixture", "type": "function_call", "call_id": "call_local_fixture",
                        "name": name, "arguments": "", "status": "in_progress"}
                yield sse("response.created", {"type": "response.created", "response": response, "sequence_number": 0})
                yield sse("response.output_item.added", {"type": "response.output_item.added",
                          "output_index": 0, "item": item, "sequence_number": 1})
                yield sse("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta",
                          "output_index": 0, "item_id": item["id"], "delta": arguments, "sequence_number": 2})
                await self._pause(row)
                yield sse("response.function_call_arguments.done", {"type": "response.function_call_arguments.done",
                          "output_index": 0, "item_id": item["id"], "arguments": arguments, "sequence_number": 3})
                item = {**item, "arguments": arguments, "status": "completed"}
                yield sse("response.output_item.done", {"type": "response.output_item.done",
                          "output_index": 0, "item": item, "sequence_number": 4})
                response = {**response, "status": "completed", "output": [item],
                            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
                                      "input_tokens_details": {"cached_tokens": 0},
                                      "output_tokens_details": {"reasoning_tokens": 0}}}
                yield sse("response.completed", {"type": "response.completed",
                          "response": response, "sequence_number": 5})
                row.completed = True
                return
            response = {"id": "resp_local_fixture", "object": "response", "created_at": 1, "status": "in_progress",
                        "model": row.body["model"], "output": [], "error": None, "incomplete_details": None}
            item = {"id": "msg_local_fixture", "type": "message", "status": "in_progress", "role": "assistant", "content": []}
            part = {"type": "output_text", "text": "", "annotations": []}
            yield sse("response.created", {"type": "response.created", "response": response, "sequence_number": 0})
            yield sse("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": item, "sequence_number": 1})
            yield sse("response.content_part.added", {"type": "response.content_part.added", "output_index": 0, "item_id": item["id"], "content_index": 0, "part": part, "sequence_number": 2})
            yield sse("response.output_text.delta", {"type": "response.output_text.delta", "output_index": 0, "item_id": item["id"], "content_index": 0, "delta": TEXT, "sequence_number": 3})
            await self._pause(row)
            part = {**part, "text": TEXT}
            item = {**item, "status": "completed", "content": [part]}
            yield sse("response.output_text.done", {"type": "response.output_text.done", "output_index": 0, "item_id": item["id"], "content_index": 0, "text": TEXT, "sequence_number": 4})
            yield sse("response.content_part.done", {"type": "response.content_part.done", "output_index": 0, "item_id": item["id"], "content_index": 0, "part": part, "sequence_number": 5})
            yield sse("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item, "sequence_number": 6})
            response = {**response, "status": "completed", "output": [item],
                        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
                                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}
            yield sse("response.completed", {"type": "response.completed", "response": response, "sequence_number": 7})
            row.completed = True
        finally:
            row.closed.set()

    def queue_tool_call(self, key, *, name, arguments):
        """Script one actual SDK function call, then return to ordinary text SSE."""
        with self.condition:
            if key not in self.keys or key in self.tool_calls:
                raise ValueError("fixture key unknown or already has a queued tool call")
            self.tool_calls[key] = (name, json.dumps(arguments, separators=(",", ":")))

    def hold(self, key):
        with self.condition:
            self.held_keys.add(key)

    def allow_key(self, key):
        if not key.startswith("sk-fake-acceptance-"):
            raise ValueError("only synthetic local fixture keys may be admitted")
        with self.condition:
            self.keys.add(key)

    def release(self, key=None):
        with self.condition:
            if key is None:
                self.held_keys.clear()
            else:
                self.held_keys.discard(key)
            for row in self.records:
                if key is None or row.key == key:
                    row.hold.set()

    def wait_request(self, key, *, offset=0, timeout=30, primary=False):
        # Native memory extraction uses record_deltas, not the main tool roster.
        def matches(row):
            return row.key == key and (not primary or any(
                tool.get("name") == "bash" for tool in row.body.get("tools", [])))
        with self.condition:
            if not self.condition.wait_for(lambda: any(matches(row) for row in self.records[offset:]), timeout):
                raise AssertionError("real provider request never reached local official-wire server")
            return next(row for row in self.records[offset:] if matches(row))

    def close(self):
        self.release()
        if self.server is not None:
            self.server.should_exit = True
        self.thread.join(10)
        self.listener.close()
        if self.thread.is_alive():
            raise RuntimeError("owned local official-wire server leaked")
        if self.error is not None:
            raise RuntimeError("owned local official-wire server failed") from self.error
