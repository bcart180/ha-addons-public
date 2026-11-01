# Simulates the Supervisor ingress proxy: strips /api/hassio_ingress/<token> and forwards.
import http.server, urllib.request, sys
PFX = "/api/hassio_ingress/tok3n_ABC"
class P(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if not self.path.startswith(PFX + "/"):
            self.send_error(404, "outside ingress prefix"); return
        sub = self.path[len(PFX):]
        req = urllib.request.Request("http://127.0.0.1:8099" + sub, headers={"X-Ingress-Path": PFX})
        try: r = urllib.request.urlopen(req); code = r.status
        except urllib.error.HTTPError as e: r = e; code = e.code
        body = r.read(); self.send_response(code)
        for k in ("Content-Type", "Cache-Control"):
            if r.headers.get(k): self.send_header(k, r.headers[k])
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
http.server.ThreadingHTTPServer(("127.0.0.1", 8123), P).serve_forever()
