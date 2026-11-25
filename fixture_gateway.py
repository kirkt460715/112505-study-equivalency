"""文旅活动的本地联调数据入口与合作方回执桩。"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from threading import Lock
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,96}")


def load_fixtures(folder):
    registers = json.loads((folder / "registers.json").read_text(encoding="utf-8"))
    records = [json.loads(line) for line in (folder / "events.ndjson").read_text(encoding="utf-8").splitlines()]
    examples = json.loads((folder / "receipts.json").read_text(encoding="utf-8"))
    if not isinstance(registers, dict):
        raise ValueError("台账根对象应为 JSON 对象")
    if any(event["cursor"] != i or not isinstance(event["payload"], dict)
           for i, event in enumerate(records, 1)):
        raise ValueError("消息游标应从 1 开始连续编号")
    replies = {}
    for entry in examples:
        signature = (entry["operation"], entry["recipient"])
        if signature in replies or entry["delivery"] not in {"applied", "lost_reply"}:
            raise ValueError("回执条目重复或传输结果无效")
        replies[signature] = entry
    return registers, records, replies


class FixtureGateway(ThreadingHTTPServer):
    def __init__(self, port, folder=FIXTURES):
        self.registers, self.records, self.replies = load_fixtures(folder)
        self.requests = {}
        self.lock = Lock()
        super().__init__(("127.0.0.1", port), FixtureHandler)


class FixtureHandler(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/v1/registers":
            self.reply(200, self.server.registers)
            return
        if parts.path == "/v1/events":
            try:
                params = parse_qs(parts.query)
                cursor = int(params.get("after", ["0"])[0])
                limit = int(params.get("limit", ["50"])[0])
                if cursor < 0 or not 1 <= limit <= 100:
                    raise ValueError
            except ValueError:
                self.reply(400, {"code": "invalid_cursor_or_limit"})
                return
            rows = self.server.records[cursor:cursor + limit]
            next_cursor = rows[-1]["cursor"] if rows else cursor
            self.reply(200, {"events": rows, "next_cursor": next_cursor})
            return
        token = parts.path.removeprefix("/v1/receipts/")
        if token != parts.path and IDENTIFIER.fullmatch(token):
            with self.server.lock:
                entry = self.server.requests.get(token)
            if entry is not None:
                self.reply(200, {"reference": token, "receipt": entry[1]})
                return
        self.reply(404, {"code": "not_found"})

    def do_POST(self):
        if self.path != "/v1/receipts":
            self.reply(404, {"code": "not_found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 32_768:
                raise ValueError
            request = json.loads(self.rfile.read(size))
            reference, operation, recipient = (request[k] for k in ("reference", "operation", "recipient"))
            if not all(isinstance(v, str) and IDENTIFIER.fullmatch(v) for v in (reference, operation, recipient)):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            self.reply(400, {"code": "invalid_receipt_request"})
            return
        signature = (operation, recipient)
        with self.server.lock:
            previous = self.server.requests.get(reference)
            example = self.server.replies.get(signature)
            if previous is None and example:
                self.server.requests[reference] = (signature, example["receipt"])
        if previous:
            if previous[0] != signature:
                self.reply(409, {"code": "reference_reused"})
            else:
                self.reply(200, {"reference": reference, "receipt": previous[1]})
        elif not example:
            self.reply(422, {"code": "no_counterparty_example"})
        elif example["delivery"] == "lost_reply":
            self.close_connection = True
        else:
            self.reply(200, {"reference": reference, "receipt": example["receipt"]})

    def log_message(self, *args):
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    options = parser.parse_args()
    with FixtureGateway(options.port, options.fixtures) as server:
        print(f"联调入口 http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
