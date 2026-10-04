"""A local stand-in for Jev's one endpoint, for the web-sieve relevance tests.

Modelled on the jev client's own fake server: HTTP/1.1 with keep-alive,
one thread per connection, every request recorded (path, headers, parsed
body). Each request is served by, in order: `rule(body)` when it returns a
step, else the next step of `script`, else `default`. A step is a dict:

    {"status": 429, "delay": 0.5, "headers": {...}, "body": {...} | "text"}
    {"close": True}                 drop the connection without a response
    {"model": "jev-x"}              answer normally but report this model
    {"mutate": fn}                  answer normally, then answers = fn(answers)

A step without "body" is answered automatically: each Noul gets the
probability written in its window's text as a marker [[p=0.83]] (the first
marker in the text), or 0.05 when there is none.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MARKER = re.compile(r"\[\[p=([0-9.]+)\]\]")
NO_MARKER_P = 0.05


def marker_answers(body: dict) -> dict:
    windows = ((body or {}).get("state") or {}).get("windows") or {}
    answers = {}
    for qid in (body or {}).get("questions") or {}:
        m = MARKER.search((windows.get(qid) or {}).get("text", ""))
        answers[qid] = {"type": "noul", "noul": float(m.group(1)) if m else NO_MARKER_P}
    return answers


class FakeJev:
    def __init__(self):
        self.script: list = []
        self.default: dict = {}
        self.rule = None
        self.requests: list = []
        self.active = 0                # requests being handled now
        self.max_active = 0            # the most handled at once
        self.finished: list = []       # question ids of each answered request, in completion order
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # quiet
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                with fake.lock:
                    fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                    fake.active += 1
                    fake.max_active = max(fake.max_active, fake.active)
                    step = fake.rule(body) if fake.rule else None
                    if step is None:
                        step = fake.script.pop(0) if fake.script else dict(fake.default)
                try:
                    self.respond(body, step)
                finally:
                    with fake.lock:
                        fake.active -= 1
                        fake.finished.append(sorted((body or {}).get("questions") or {}))

            def respond(self, body, step):
                if step.get("delay"):
                    time.sleep(step["delay"])
                if step.get("close"):
                    self.close_connection = True
                    return
                if "body" in step:
                    payload = step["body"]
                else:
                    answers = marker_answers(body)
                    if step.get("mutate"):
                        answers = step["mutate"](answers)
                    payload = {"model": step.get("model", "jev-1.13.0"), "answers": answers,
                               "usage": {"input_tokens": 100, "output_tokens": 0}}
                data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                try:
                    self.send_response(step.get("status", 200))
                    for k, v in (step.get("headers") or {}).items():
                        self.send_header(k, v)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass  # the client gave up on this request (a timeout)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def asked_ids(self) -> list:
        """Question ids of every recorded request, in arrival order."""
        return [sorted((r["body"] or {}).get("questions") or {}) for r in self.requests]
