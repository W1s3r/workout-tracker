"""Fake servers for tests (stdlib http.server): an OpenAI-compatible LLM API
and a small 'intranet' with a poorly-ranked keyword search page."""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLLMServer:
    """responder(body: dict) -> {"content": str, "tool_calls": [{"name", "arguments"}]}"""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.endswith("/models"):
                    self._json({"data": [{"id": "fake-model"}]})
                else:
                    self.send_error(404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                outer.requests.append(body)
                r = outer.responder(body)
                tcs = [{"id": "call_%d" % i, "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}))}}
                       for i, c in enumerate(r.get("tool_calls", []))]
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    text = r.get("content", "")
                    for i in range(0, len(text), 7):
                        self._sse({"choices": [{"index": 0, "delta": {"content": text[i:i + 7]}}]})
                    for i, tc in enumerate(tcs):
                        args = tc["function"]["arguments"]
                        half = len(args) // 2
                        self._sse({"choices": [{"index": 0, "delta": {"tool_calls": [
                            {"index": i, "id": tc["id"], "type": "function",
                             "function": {"name": tc["function"]["name"], "arguments": args[:half]}}]}}]})
                        self._sse({"choices": [{"index": 0, "delta": {"tool_calls": [
                            {"index": i, "function": {"arguments": args[half:]}}]}}]})
                    self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tcs else "stop"}]})
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                msg = {"role": "assistant", "content": r.get("content", "")}
                if tcs:
                    msg["tool_calls"] = tcs
                self._json({"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

            def _sse(self, obj):
                self.wfile.write(("data: %s\n\n" % json.dumps(obj)).encode())
                self.wfile.flush()

            def _json(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


DOCS = {
    "1": ("Project Falcon Overview", "Project Falcon is a logistics modernization effort. "
          "The program manager is Jane Smith. Falcon replaces the legacy LOGTRAK system. "
          "See also the <a href='/doc/3'>Falcon schedule memo</a>."),
    "2": ("Cafeteria menu", "Tuesday is taco day. Falcon-shaped cookies available."),
    "3": ("Falcon schedule memo", "Initial operating capability (IOC) for Project Falcon is March 2027. "
          "Full operating capability is planned for 2028."),
    "4": ("LOGTRAK retirement notice", "LOGTRAK will be retired after Falcon reaches IOC."),
}


class FakeIntranet:
    def __init__(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                if u.path == "/search":
                    q = urllib.parse.parse_qs(u.query).get("q", [""])[0].lower()
                    words = [w for w in q.replace('"', "").split() if w]
                    hits = [k for k, (t, b) in DOCS.items()
                            if any(w in (t + " " + b).lower() for w in words)]
                    hits.sort()  # deliberately poor ranking
                    items = "".join("<div class='r'><a href='/doc/%s'>%s</a><p>%s</p></div>" % (
                        k, DOCS[k][0], DOCS[k][1][:80]) for k in hits)
                    page = ("<html><body><nav><a href='/'>Home</a><a href='/help'>Help</a></nav>"
                            "<h1>Results</h1>%s<a href='/search?q=x&page=2'>Next</a></body></html>" % items)
                elif u.path == "/jsearch":
                    # results are built by JavaScript - invisible to plain HTTP clients
                    q = urllib.parse.parse_qs(u.query).get("q", [""])[0]
                    page = ("<html><head><title>App</title><script>window.onload = function(){"
                            "setTimeout(function(){var d=document.getElementById('r');"
                            "d.innerHTML=\"<a href='/doc/1'>Project Falcon Overview</a><p>Falcon logistics modernization "
                            "program overview</p><a href='/doc/3'>Falcon schedule memo</a><p>IOC is March 2027</p>"
                            "<p>query was " + "\" + q + \"</p>\";}, 300);};</script></head>"
                            "<body><div id='r'></div><noscript>enable js</noscript></body></html>").replace(
                        "\" + q + \"", q.replace("<", ""))
                elif u.path == "/login-wall":
                    page = ("<html><head><title>Sign In - Portal</title></head><body><form>"
                            "<input name=u><input type=password name=p></form></body></html>")
                elif u.path == "/file.pdf":
                    data = b"%PDF-1.4 fake pdf bytes \x00\x01\x02"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                elif u.path.startswith("/doc/"):
                    k = u.path.split("/")[-1]
                    if k not in DOCS:
                        self.send_error(404)
                        return
                    page = "<html><head><title>%s</title></head><body><h1>%s</h1><p>%s</p></body></html>" % (
                        DOCS[k][0], DOCS[k][0], DOCS[k][1])
                else:
                    self.send_error(404)
                    return
                data = page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
