"""Gate-only static fixture. Fails rather than choosing a different port."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import os


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h1>Gate browser fixture</h1>")


HTTPServer((os.environ["HOST"], int(os.environ["PORT"])), Page).serve_forever()
