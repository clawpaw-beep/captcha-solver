import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT = os.path.dirname(os.path.abspath(__file__))

class H(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_POST(self):
        name = self.path.strip("/").split("?")[0].split("/")[-1] or "shot"
        n = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(n)
        with open(os.path.join(OUT, f"page_{name}.png"), "wb") as fh:
            fh.write(data)
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "text/plain"); self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *a):
        pass

ThreadingHTTPServer(("127.0.0.1", 8812), H).serve_forever()
