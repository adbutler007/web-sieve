"""A local stand-in for Jina Reader (r.jina.ai), for the web-sieve fetch tests.

The tests point web-sieve's JINA_BASE at this server, so a request for
https://example.com/a arrives as GET /https://example.com/a. Every request
is recorded (path, target URL, headers with lower-cased names). Each request
is served by, in order: `rule(url, headers)` when it returns a step, else the
next step of `script`, else `default`. A step is a dict:

    {"status": 429, "headers": {"Retry-After": "7"}}   an HTTP error
    {"body": "Title: ...\\n\\nMarkdown Content:\\n..."}  text served with status 200
    {"raw": b"..."}                                  bytes served as they are
    {"truncate": True}                               Content-Length 100 bytes larger than the body, then close
    {"close": True}                                  close the connection without a response
    {"delay": 0.5}                                   wait before answering (combine with any of the above)

A step without "body" or "raw" serves page(url): a normal page of about
800 characters. The constants below are bodies for the failure kinds.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FILLER = ("Widgets are installed with the widgetctl tool, which reads its settings from a plain text file. "
          "Each setting has a name, a value and an optional comment explaining why it was chosen. ")


def jina(url: str, title: str, body: str, warnings: tuple = ()) -> str:
    """A response in Jina Reader's default text format: the preamble, then the markdown."""
    head = f"Title: {title}\n\nURL Source: {url}\n\n"
    head += "".join(f"Warning: {w}\n" for w in warnings) + ("\n" if warnings else "")
    return head + "Markdown Content:\n" + body


def page(url: str, title: str = "Widget guide") -> str:
    """A normal page: well over the thin limit, no challenge phrase."""
    return jina(url, title, "# Widget guide\n\n" + FILLER * 5 + "\n")


CHALLENGE = jina("https://example.com/x", "Just a moment...",
                 "## Performing security verification\n\nThis website uses a security service to protect "
                 "against malicious bots.\n", ("This page maybe requiring CAPTCHA, please make sure you are "
                                               "authorized to access this page.",))
EMPTY = jina("https://example.com/x", "", "\n")
INVALID_UTF8 = (b"Title: Bytes\n\nURL Source: https://example.com/b\n\nMarkdown Content:\n"
                + FILLER.encode() * 4 + b"bad \xff\xfe bytes and \xc3 one more\n")


class FakeJina:
    def __init__(self):
        self.script: list = []
        self.default: dict = {}
        self.rule = None
        self.requests: list = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # quiet
                pass

            def do_GET(self):
                url = self.path[1:]
                headers = {k.lower(): v for k, v in self.headers.items()}
                with fake.lock:
                    fake.requests.append({"path": self.path, "url": url, "headers": headers})
                    step = fake.rule(url, headers) if fake.rule else None
                    if step is None:
                        step = fake.script.pop(0) if fake.script else dict(fake.default)
                try:
                    self.respond(url, step)
                except OSError:
                    pass  # the client gave up on this request (a timeout)

            def respond(self, url, step):
                if step.get("delay"):
                    time.sleep(step["delay"])
                if step.get("close"):
                    self.close_connection = True
                    return
                status = step.get("status", 200)
                if "raw" in step:
                    data = step["raw"]
                elif "body" in step:
                    data = step["body"].encode()
                elif status == 200:
                    data = page(url).encode()
                else:
                    data = b'{"code": %d, "message": "fake error"}' % status
                self.send_response(status)
                for k, v in (step.get("headers") or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data) + (100 if step.get("truncate") else 0)))
                self.end_headers()
                self.wfile.write(data)
                if step.get("truncate"):
                    self.wfile.flush()
                    self.close_connection = True

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

    def urls(self) -> list:
        """Target URL of every recorded request, in arrival order."""
        return [r["url"] for r in self.requests]
