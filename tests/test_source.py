import json
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from source_service import create_source


class WaterTest(unittest.TestCase):
    def setUp(self):
        self.server = create_source()
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def exchange(self, path, payload=None, headers=None):
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                          headers=headers or {})
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            if response.headers["Content-Type"].startswith("application/json"):
                body = json.loads(body)
            return response.code, response.headers, body

    def advance(self, clock):
        self.assertEqual(self.exchange("/clock", {"time": clock})[0], 200)

    def test_radio_has_physical_cutoffs_and_explicit_missing_ranges(self):
        state = self.exchange("/radio?cab=M1")[2]
        self.assertEqual(state["gaps"][0]["state"], "wait_relay")
        self.assertEqual(self.exchange("/download?slot=R1&page=A-Mid")[0], 503)
        self.assertEqual(self.exchange("/download?slot=T1&page=A-In")[0], 200)
        self.assertEqual(self.exchange("/download?slot=T1&page=A-In")[0], 429)
        self.advance("09:10:00")
        self.assertEqual(self.exchange("/download?slot=R1&page=A-Mid")[2]["range"], [5, 6])
        self.advance("09:15:00")
        fixed = self.exchange("/download?slot=R2&page=A-Fix")[2]
        self.assertEqual((fixed["epoch"], fixed["range"]), ("boot-A", [1, 4]))
        radio = self.exchange("/radio?cab=M1")[2]["pages"]
        self.assertEqual(next(p["replaces"] for p in radio if p["id"] == "A-Fix"), "A-In")
        self.advance("09:26:00")
        source = self.exchange("/station?station=Q8")[2]["events"]
        self.assertEqual({(row["epoch"],row["id"]) for row in source}, {("boot-A","T1"),("boot-B","T1")})

    def test_committed_trip_and_late_field_receipts_are_distinct(self):
        record = {"request":"test-slot","trip":"RIVER-1","station":"Q4","event":"STORM7",
                  "phase":"peak","bottle_set":"V1"}
        self.assertEqual(self.exchange("/trip/book", record)[0], 503)
        self.assertEqual(self.exchange("/trip/receipt?request=test-slot")[0], 404)
        self.assertEqual(self.exchange("/trip/book", {**record,"request":"another"})[0], 409)
        self.advance("09:14:00")
        self.assertEqual(self.exchange("/trip/receipt?request=test-slot")[2]["arrival"], "09:15:00")
        self.assertEqual(self.exchange("/weather")[2]["entries"][1]["replaces"], "F7")
        self.assertNotIn("S2", self.exchange("/boxes")[2]["entries"][-1]["blank"]["covers"])
        self.advance("09:25:00")
        self.assertEqual(self.exchange("/trip/receipt?request=test-slot")[2]["actual"]["time"], "09:18:00")
        entries = self.exchange("/lab")[2]["entries"]
        self.assertEqual([item["state"] for item in entries], ["rejected", "received", "field_sample"])
