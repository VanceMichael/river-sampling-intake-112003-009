from pathlib import Path
from threading import Thread
import json
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from source_service import create_source


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.server = create_source()
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def source_response(self, path):
        try:
            with urlopen(self.base + path, timeout=2) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def test_input_documents(self):
        for name in ['weather', 'clocks', 'boxes', 'duty', 'dispatch']:
            with self.subTest(document=name):
                value = Path("data", name + ".json").read_text(encoding="utf-8")
                self.assertTrue(json.loads(value))

    def test_receiving_responses(self):
        status, catalog = self.source_response('/cab/catalog')
        self.assertEqual(status, 200)
        self.assertIn('sources', json.loads(catalog))
        status, payload = self.source_response('/cab/repair-M1-3.json')
        self.assertEqual(status, 404)
        self.assertTrue(payload)
        status, payload = self.source_response('/cab/repair-M1-3.json')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)['evidence'], 'camera-44')

