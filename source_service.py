"""本地资料端，按原件路径和演练回放提供逐次响应。"""
from argparse import ArgumentParser
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent


def create_source(port=0, root=ROOT):
    routes = json.loads((root / "responses.json").read_text(encoding="utf-8"))
    calls = Counter()

    class Source(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_body(self, status, body, kind, headers=()):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False).encode()
            self.send_body(status, encoded, "application/json; charset=utf-8")

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                self.send_json(200, {"paths": list(routes), "records": "/data/"})
                return
            if path in routes:
                sequence = routes[path]
                index = min(calls[path], len(sequence) - 1)
                calls[path] += 1
                status, payload = sequence[index]
                if isinstance(payload, str):
                    self.send_body(status, payload.encode(), "text/plain; charset=utf-8")
                else:
                    self.send_json(status, payload)
                return
            parts = path.lstrip("/").split("/")
            prefix = "data" if parts[0] == "data" else "originals"
            relative = parts[1:] if prefix == "data" else parts
            if relative and all(part and part not in {".", ".."} for part in relative):
                document = (root / prefix).joinpath(*relative)
                if document.is_file():
                    kind = ("application/json" if document.suffix == ".json" else "text/plain")
                    self.send_body(200, document.read_bytes(), kind + "; charset=utf-8")
                    return
            self.send_json(404, {"state": "not_found"})

    return HTTPServer(("127.0.0.1", port), Source)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8040)
    args = parser.parse_args()
    with create_source(args.port) as server:
        print(f"资料服务在 http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
