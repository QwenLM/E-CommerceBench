#!/usr/bin/env python3
"""HTTP adapter for driving E-Commerce Bench from an external agent runtime."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.external_runtime import (
    ExternalAgentSession,
    ExternalRuntimeError,
    SessionStateError,
)

MAX_REQUEST_BYTES = 1024 * 1024


class ExternalAgentHandler(BaseHTTPRequestHandler):
    server_version = "ECommerceBenchExternalAgent/1"

    @property
    def session(self) -> ExternalAgentSession:
        return self.server.session  # type: ignore[attr-defined]

    @property
    def access_token(self) -> str:
        return self.server.access_token  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/health":
                self._send(HTTPStatus.OK, {"status": "ok"})
                return
            self._authorize()
            if path == "/v1/session":
                self._send(HTTPStatus.OK, self.session.descriptor())
            elif path == "/v1/result":
                self._send(HTTPStatus.OK, self.session.result())
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except SessionStateError as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except ExternalRuntimeError as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except PermissionError as exc:
            self._send(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
        except Exception:
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal server error"},
            )

    def do_POST(self) -> None:
        try:
            self._authorize()
            path = urlparse(self.path).path
            if path == "/v1/finish":
                self.session.close()
                self._send(HTTPStatus.OK, self.session.result())
                return
            if path != "/v1/actions":
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            body = self._read_json()
            self._send(
                HTTPStatus.OK,
                self.session.act(
                    body.get("tool_calls"),
                    content=body.get("content", ""),
                    reasoning_content=body.get("reasoning_content"),
                ),
            )
        except SessionStateError as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except ExternalRuntimeError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except PermissionError as exc:
            self._send(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
        except Exception:
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal server error"},
            )

    def _authorize(self) -> None:
        prefix = "Bearer "
        value = self.headers.get("Authorization", "")
        token = value[len(prefix) :] if value.startswith(prefix) else ""
        if not secrets.compare_digest(
            token.encode("utf-8"), self.access_token.encode("utf-8")
        ):
            raise PermissionError("invalid access token")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ExternalRuntimeError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ExternalRuntimeError(
                f"request body must be between 1 and {MAX_REQUEST_BYTES} bytes"
            )
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ExternalRuntimeError("request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ExternalRuntimeError("request body must be a JSON object")
        return value

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve one E-Commerce Bench episode to an external agent"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--token",
        help="Bearer token (default: generate a random token and print it once)",
    )
    parser.add_argument("--max-turns", type=int, default=4000)
    parser.add_argument("--max-days", type=int, default=365)
    parser.add_argument("--initial-balance", type=float, default=100000.0)
    parser.add_argument("--daily-fee", type=float, default=50.0)
    parser.add_argument("--log-dir")
    parser.add_argument("--run-index", type=int, default=0)
    args = parser.parse_args()

    token = args.token or secrets.token_urlsafe(32)
    session = ExternalAgentSession(
        max_turns=args.max_turns,
        max_day=args.max_days,
        initial_balance=args.initial_balance,
        daily_fee=args.daily_fee,
        log_dir=args.log_dir,
        run_index=args.run_index,
    )
    server = ThreadingHTTPServer((args.host, args.port), ExternalAgentHandler)
    server.session = session  # type: ignore[attr-defined]
    server.access_token = token  # type: ignore[attr-defined]
    print(
        json.dumps(
            {
                "status": "ready",
                "base_url": f"http://{args.host}:{server.server_port}",
                "token": token,
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        session.close()
        server.server_close()


if __name__ == "__main__":
    main()
