"""Лек HTTP сървър, симулиращ Stripe Checkout API (/v1/checkout/sessions) за E2E тестове."""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

_LOCK = threading.Lock()
_SESSIONS: dict[str, dict[str, Any]] = {}
_IDEMPOTENCY_CACHE: dict[str, dict[str, Any]] = {}
_CREATE_CALLS: list[dict[str, Any]] = []
_EXPIRE_CALLS: list[str] = []
_CONTROL: dict[str, Any] = {
    "drop_next_create_after_commit": False,
    "delay_next_create_seconds": 0.0,
    "fail_next_expire": False,
}


def _compute_total_from_form(form: dict[str, list[str]]) -> tuple[int, str]:
    total_minor = 0
    currency = "eur"
    idx = 0
    while True:
        unit_key = f"line_items[{idx}][price_data][unit_amount]"
        qty_key = f"line_items[{idx}][quantity]"
        cur_key = f"line_items[{idx}][price_data][currency]"
        if unit_key not in form:
            break
        unit_amount = int((form.get(unit_key) or ["0"])[0])
        quantity = int((form.get(qty_key) or ["1"])[0])
        if cur_key in form:
            currency = (form[cur_key][0] or "eur").lower()
        total_minor += unit_amount * quantity
        idx += 1
    return total_minor, currency


class _StripeMockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return dict(json.loads(self.rfile.read(length).decode("utf-8")))

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
            return

        if self.path == "/_sessions":
            with _LOCK:
                snapshot = {
                    "sessions": list(_SESSIONS.values()),
                    "create_calls": list(_CREATE_CALLS),
                    "expire_calls": list(_EXPIRE_CALLS),
                    "idempotency_keys": list(_IDEMPOTENCY_CACHE.keys()),
                }
            self._write_json(200, snapshot)
            return

        if self.path.startswith("/v1/checkout/sessions/"):
            session_id = self.path.removeprefix("/v1/checkout/sessions/").split("?")[0]
            with _LOCK:
                session = _SESSIONS.get(session_id)
                if session is None:
                    self._write_json(
                        404,
                        {
                            "error": {
                                "message": "No such session",
                                "type": "invalid_request_error",
                            }
                        },
                    )
                    return
                payload = dict(session)
            self._write_json(200, payload)
            return

        self._write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/_reset":
            with _LOCK:
                _SESSIONS.clear()
                _IDEMPOTENCY_CACHE.clear()
                _CREATE_CALLS.clear()
                _EXPIRE_CALLS.clear()
                _CONTROL["drop_next_create_after_commit"] = False
                _CONTROL["delay_next_create_seconds"] = 0.0
                _CONTROL["fail_next_expire"] = False
            self._write_json(200, {"status": "reset"})
            return

        if self.path == "/_control":
            body = self._read_json()
            with _LOCK:
                for key in (
                    "drop_next_create_after_commit",
                    "delay_next_create_seconds",
                    "fail_next_expire",
                ):
                    if key in body:
                        _CONTROL[key] = body[key]
            self._write_json(200, dict(_CONTROL))
            return

        if self.path == "/_control/set_session":
            body = self._read_json()
            session_id = str(body["session_id"])
            with _LOCK:
                session = _SESSIONS.get(session_id)
                if session is None:
                    self._write_json(404, {"error": "session_not_found"})
                    return
                if "status" in body:
                    session["status"] = body["status"]
                if "payment_status" in body:
                    session["payment_status"] = body["payment_status"]
                payload = dict(session)
            self._write_json(200, payload)
            return

        if self.path == "/v1/checkout/sessions":
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            form = parse_qs(raw_body)
            idempotency_key = self.headers.get("Idempotency-Key") or uuid.uuid4().hex

            with _LOCK:
                delay_seconds = float(_CONTROL.get("delay_next_create_seconds") or 0.0)
                if delay_seconds > 0:
                    _CONTROL["delay_next_create_seconds"] = 0.0
            if delay_seconds > 0:
                time.sleep(delay_seconds)

            drop_response = False
            with _LOCK:
                _CREATE_CALLS.append({"idempotency_key": idempotency_key})
                cached = _IDEMPOTENCY_CACHE.get(idempotency_key)
                if cached is not None:
                    session_payload = dict(cached)
                else:
                    session_id = f"cs_test_mock_{uuid.uuid4().hex[:16]}"
                    total_minor, currency = _compute_total_from_form(form)
                    order_id = (
                        form.get("client_reference_id")
                        or form.get("metadata[order_id]")
                        or [""]
                    )[0]
                    attempt_key = (form.get("metadata[attempt_key]") or [""])[0]
                    session_payload = {
                        "id": session_id,
                        "object": "checkout.session",
                        "status": "open",
                        "payment_status": "unpaid",
                        "url": f"https://checkout.stripe.mock/pay/{session_id}",
                        "amount_total": total_minor,
                        "currency": currency,
                        "livemode": False,
                        "client_reference_id": order_id,
                        "metadata": {
                            "order_id": order_id,
                            "attempt_key": attempt_key,
                        },
                    }
                    _SESSIONS[session_id] = session_payload
                    _IDEMPOTENCY_CACHE[idempotency_key] = dict(session_payload)
                    if _CONTROL.get("drop_next_create_after_commit"):
                        _CONTROL["drop_next_create_after_commit"] = False
                        drop_response = True

            if drop_response:
                # Симулираме загубен HTTP отговор след създаване на сесията в Stripe!
                self.close_connection = True
                self.connection.close()
                return

            self._write_json(200, session_payload)
            return

        if self.path.startswith("/v1/checkout/sessions/") and self.path.endswith(
            "/expire"
        ):
            session_id = self.path.removeprefix("/v1/checkout/sessions/").removesuffix(
                "/expire"
            )
            with _LOCK:
                if _CONTROL.get("fail_next_expire"):
                    _CONTROL["fail_next_expire"] = False
                    self._write_json(
                        500,
                        {
                            "error": {
                                "message": "Simulated Stripe expire outage",
                                "type": "api_error",
                            }
                        },
                    )
                    return

                _EXPIRE_CALLS.append(session_id)
                session = _SESSIONS.get(session_id)
                if session is None:
                    self._write_json(
                        404,
                        {
                            "error": {
                                "message": "No such session",
                                "type": "invalid_request_error",
                            }
                        },
                    )
                    return
                session["status"] = "expired"
                payload = dict(session)
            self._write_json(200, payload)
            return

        self._write_json(404, {"error": "not_found"})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8081), _StripeMockHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
