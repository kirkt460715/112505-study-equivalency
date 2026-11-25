from http.client import HTTPConnection, RemoteDisconnected
import json
from threading import Thread
import unittest

from fixture_gateway import FixtureGateway


class FixtureGatewayTest(unittest.TestCase):
    def setUp(self):
        self.server = FixtureGateway(0)
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, payload=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            body = None if payload is None else json.dumps(payload)
            connection.request(method, path, body, {"Content-Type": "application/json"})
            result = connection.getresponse()
            return result.status, json.loads(result.read())
        finally:
            connection.close()

    def test_registers_and_event_paging(self):
        status, registers = self.call("GET", "/v1/registers")
        self.assertEqual(status, 200)
        self.assertTrue(registers)
        first_status, first = self.call("GET", "/v1/events?limit=1")
        self.assertEqual(first_status, 200)
        self.assertEqual(first["next_cursor"], 1)
        next_status, later = self.call("GET", "/v1/events?after=1")
        self.assertEqual(next_status, 200)
        self.assertGreaterEqual(len(later["events"]), 2)
        self.assertEqual(first["events"] + later["events"], self.server.records)
        self.assertEqual(self.call("GET", "/v1/events?after=-1")[0], 400)

    def test_receipt_lookup_and_protected_retry(self):
        self.assertTrue(self.server.replies)
        for index, ((action, recipient), sample) in enumerate(self.server.replies.items()):
            with self.subTest(operation=action, recipient=recipient):
                payload = {"reference": f"local-{index}", "operation": action, "recipient": recipient}
                location = "/v1/receipts/" + payload["reference"]
                expected = (200, {"reference": payload["reference"], "receipt": sample["receipt"]})
                self.assertEqual(self.call("GET", location)[0], 404)
                if sample["delivery"] == "lost_reply":
                    with self.assertRaises(RemoteDisconnected):
                        self.call("POST", "/v1/receipts", payload)
                else:
                    self.assertEqual(self.call("POST", "/v1/receipts", payload), expected)
                self.assertEqual(self.call("GET", location), expected)
                self.assertEqual(self.call("POST", "/v1/receipts", payload), expected)
                self.assertEqual(self.call("POST", "/v1/receipts", {**payload, "recipient": "other"})[0], 409)


if __name__ == "__main__":
    unittest.main()
