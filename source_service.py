"""监测站摘要、机箱无线记录和巡河车台。"""
from argparse import ArgumentParser
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent


def create_source(port=0, root=ROOT):
    def read(name):
        return json.loads((root / name).read_text())

    radio = read("data/radio.json")
    dispatch = read("data/dispatch.json")
    trips = {row["trip"]: row for row in dispatch["trips"]}
    spent = {}
    claims = {}
    now = radio["initial"]

    class Water(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value, data_digest=False):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if data_digest:
                self.send_header("X-Content-SHA256", sha256(data).hexdigest())
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlsplit(self.path)
            q = {key: item[0] for key, item in parse_qs(url.query).items()}
            try:
                return self.route(url.path, q)
            except (KeyError, ValueError, IndexError):
                return self.reply(400, {"state": "invalid_query"})

        def route(self, path, q):
            if path == "/":
                return self.reply(200, {"time": now, "station": "/station", "radio": "/radio", "dispatch": "/trips"})
            if path in ("/weather", "/clocks", "/boxes", "/duty"):
                values = read("data/" + path.lstrip("/") + ".json")
                return self.reply(200, {"entries": [row for row in values if row["known"] <= now]})
            if path == "/station":
                source = [row for row in radio["summaries"] if row["station"] == q["station"] and row["seen"] <= now]
                return self.reply(200, {"events": source})
            if path == "/radio":
                source = [row for row in radio["pages"] if row["cab"] == q["cab"] and row["listed"] <= now]
                gaps = [row for row in radio["gaps"] if row["cab"] == q["cab"] and row["known"] <= now]
                return self.reply(200, {"time": now, "pages": source, "gaps": gaps})
            if path == "/radio/slots":
                return self.reply(200, {"slots": [row for row in radio["slots"] if row["until"] > now]})
            if path == "/download":
                return self.download(q)
            if path == "/trips":
                return self.reply(200, {"trips": [
                    {**trip, "used": sum(item["travel"]["trip"] == trip["trip"] and item["state"] in ("accepted", "sampled")
                                        for item in claims.values())} for trip in trips.values()
                ]})
            if path == "/trip/receipt":
                receipt = claims.get(q["request"])
                if receipt and receipt["seen"] <= now:
                    return self.reply(200, receipt)
                return self.reply(404, {"state": "unconfirmed"})
            if path == "/lab":
                return self.reply(200, {"entries": [row for row in dispatch["lab"] if row["known"] <= now]})
            return self.reply(404, {"state": "unknown"})

        def download(self, q):
            windows = [item for item in radio["slots"] if item["slot"] == q["slot"] and item["from"] <= now < item["until"]]
            if not windows:
                return self.reply(503, {"state": "no_radio_slot"})
            slot = windows[0]
            counter = (slot["slot"], slot["from"])
            used = spent.get(counter, 0)
            if used >= slot["pages"]:
                return self.reply(429, {"state": "radio_quota_used"})
            for page in radio["pages"]:
                if page["id"] != q["page"] or page["channel"] != slot["channel"] or not page["listed"] <= now:
                    continue
                if now >= page["expires"]:
                    return self.reply(410, {"state": "evicted"})
                if now < page["ready"]:
                    return self.reply(425, {"state": "closing_page"})
                spent[counter] = used + 1
                return self.reply(200, read(page["file"]), data_digest=True)
            return self.reply(404, {"state": "not_declared_in_channel"})

        def do_POST(self):
            nonlocal now
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/clock":
                    if not isinstance(body["time"], str) or len(body["time"]) != 8 or body["time"] < now:
                        return self.reply(409, {"state": "cannot_rewind"})
                    now = body["time"]
                    for info in dispatch["updates"]:
                        if info["seen"] > now:
                            continue
                        for record in claims.values():
                            travel = record["travel"]
                            if (travel["trip"], travel["station"]) == (info["trip"], info["station"]):
                                record.update(state=info["state"], actual=info["actual"])
                    return self.reply(200, {"time": now})
                if self.path == "/trip/book":
                    return self.book(body)
            except (ValueError, KeyError, IndexError, TypeError):
                return self.reply(400, {"state": "invalid_data"})
            return self.reply(404, {"state": "unknown"})

        def book(self, body):
            key = body["request"]
            old = claims.get(key)
            if old:
                if old["travel"] != body:
                    return self.reply(409, {"state": "different_request"})
                return self.reply(503, {"state": "reply_lost"}) if old["seen"] > now else self.reply(200, old)
            trip = trips[body["trip"]]
            if now > trip["book_by"] or body["station"] not in trip["stations"]:
                return self.reply(422, {"state": "trip_unavailable"})
            if body["bottle_set"] not in trip["box_slots"]:
                return self.reply(422, {"state": "unknown_box_slot"})
            if any(entry["travel"]["bottle_set"] == body["bottle_set"] and
                   entry["travel"]["trip"] == body["trip"] and entry["state"] in ("accepted", "sampled")
                   for entry in claims.values()):
                return self.reply(409, {"state": "box_slot_in_use"})
            record = {"state": "accepted", "travel": body, "seen": trip.get("first_visible", now),
                      "arrival": trip["stations"][body["station"]], "receipt": "TR-" + key}
            claims[key] = record
            if record["seen"] > now:
                return self.reply(503, {"state": "reply_lost"})
            return self.reply(200, record)

    return HTTPServer(("127.0.0.1", port), Water)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8040)
    with create_source(parser.parse_args().port) as server:
        print(f"监测、机箱和车台服务在 http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
