"""Local preview: serves www/ and proxies everything else to kiwix-serve (same as nginx.conf).

Usage: ssh -N -L 18080:<kiwix-container-ip>:8080 umbrel@umbrel.local &   # tunnel to the Kiwix container
       python3 dev_server.py                                   # then open http://localhost:8765
"""
import http.client
import http.server
import os
import sys

WWW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anaszaman-maktaba", "data", "www")
STATIC = {"/app.js", "/app.css", "/reader.css", "/manifest.webmanifest", "/icon.svg", "/icon-180.png", "/icon-512.png"}
UPSTREAM = ("127.0.0.1", int(os.environ.get("KIWIX_PORT", "18080")))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WWW, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html") or path in STATIC:
            if path == "/":
                self.path = "/index.html"
            return super().do_GET()
        conn = http.client.HTTPConnection(*UPSTREAM, timeout=120)
        conn.request("GET", self.path, headers={"Accept-Encoding": "identity"})
        res = conn.getresponse()
        body = res.read()
        self.send_response(res.status)
        for k, v in res.getheaders():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        super(http.server.SimpleHTTPRequestHandler, self).end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
