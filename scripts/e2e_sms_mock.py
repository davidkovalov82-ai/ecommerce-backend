"""Лек HTTP сървър, симулиращ Twilio Messages API за изолирани E2E тестове без плащания."""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

_LOCK = threading.Lock()
_SENT_MESSAGES: list[dict[str, str]] = []


class _TwilioMockHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def _write_json(self, status_code: int, payload: dict[str, object]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        if self.path == "/_sent":
            with _LOCK:
                snapshot = list(_SENT_MESSAGES)
            self._write_json(200, {"messages": snapshot})
            return
        self._write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self.path.startswith("/2010-04-01/Accounts/") or not self.path.endswith(
            "/Messages.json"
        ):
            self._write_json(404, {"error": "not_found"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body_raw = self.rfile.read(content_length).decode("utf-8")
        form = parse_qs(body_raw)

        sid = f"SM_MOCK_{uuid.uuid4().hex[:20]}"
        record = {
            "sid": sid,
            "to": (form.get("To") or [""])[0],
            "from": (form.get("From") or [""])[0],
            "body": (form.get("Body") or [""])[0],
            "idempotency_key": self.headers.get("Idempotency-Key", ""),
            "status": "queued",
        }
        with _LOCK:
            _SENT_MESSAGES.append(record)

        self._write_json(201, {"sid": sid, "status": "queued"})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8080), _TwilioMockHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
