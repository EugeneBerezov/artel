"""A fake Telegram Bot API on 127.0.0.1 for tests (reused by Task 23): records every request, answers from `responses`."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeTelegram:
    def __init__(self):
        self.paths: list[str] = []
        self.bodies: list[dict] = []
        self.responses: dict[str, tuple[int, dict]] = {
            "getMe": (200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "artel_test_bot"}}),
            "sendMessage": (200, {"ok": True, "result": {"message_id": 7}}),
        }
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.paths.append(self.path)
                outer.bodies.append(json.loads(raw.decode("utf-8")) if raw else {})
                method = self.path.rsplit("/", 1)[-1]
                code, body = outer.responses.get(method, (404, {"ok": False, "description": "Not Found"}))
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakeTelegram":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def __enter__(self) -> "FakeTelegram":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def methods(self) -> list[str]:
        return [p.rsplit("/", 1)[-1] for p in self.paths]
