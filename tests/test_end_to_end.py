"""End-to-end tests: the real app.py against a contract-accurate fake upstream.

No API key, no network access and no spend are required.
Run with: python3 -m unittest discover -s tests -v
"""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY = "test-key"

# Bypass any system proxy so the tests always talk straight to 127.0.0.1.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_ready(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with OPENER.open(url, timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def request(url: str, payload=None, method=None, raw=False):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=90) as response:
            body = response.read().decode("utf-8")
            return response.status, dict(response.headers), body
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read().decode("utf-8")


def raw_get(host: str, port: int, path: str) -> str:
    """Send an unnormalised request line so '..' survives the client side."""
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(f"GET {path} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")


class RadarEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake_port = free_port()
        cls.fake = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "fake_typesafe.py"), str(cls.fake_port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        cls.fake_base = f"http://127.0.0.1:{cls.fake_port}"
        if not wait_ready(f"{cls.fake_base}/__ping"):
            cls.tearDownClass()
            raise RuntimeError("fake upstream failed to start")

        cls.port = free_port()
        env = dict(os.environ)
        env.update(
            {
                "RADAR_HOST": "127.0.0.1",
                "RADAR_PORT": str(cls.port),
                "RADAR_API_BASE": cls.fake_base,
                "RADAR_PROXY": "direct",
            }
        )
        cls.app = subprocess.Popen(
            [sys.executable, str(ROOT / "app.py")],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        if not wait_ready(f"{cls.base}/api/config"):
            cls.tearDownClass()
            raise RuntimeError("radar server failed to start")

    @classmethod
    def tearDownClass(cls):
        for process in (getattr(cls, "app", None), getattr(cls, "fake", None)):
            if process is None:
                continue
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    # -- configuration ------------------------------------------------------

    def test_config_exposes_defaults_and_no_secrets(self):
        status, _headers, body = request(f"{self.base}/api/config")
        self.assertEqual(status, 200)
        config = json.loads(body)
        self.assertRegex(config["model"], r"^jev-\d+\.\d+\.\d+$")
        self.assertIn("jev-latest", config["knownAliases"])
        self.assertEqual(set(config["thresholds"]), set(config["thresholdBounds"]))
        self.assertEqual(config["maxRows"], 500)
        self.assertEqual(config["apiBase"], self.fake_base)
        self.assertEqual(config["proxy"], "", "RADAR_PROXY=direct must disable proxies")

    def test_threshold_defaults_include_the_review_gate(self):
        _status, _headers, body = request(f"{self.base}/api/config")
        thresholds = json.loads(body)["thresholds"]
        self.assertLess(thresholds["review_confidence"], thresholds["high_urgent"])

    # -- static assets ------------------------------------------------------

    def test_index_and_core_script_are_served(self):
        status, headers, body = request(f"{self.base}/")
        self.assertEqual(status, 200)
        self.assertIn("评论商机雷达", body)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

        status, _headers, body = request(f"{self.base}/core.js")
        self.assertEqual(status, 200)
        self.assertIn("RadarCore", body)

    def test_path_traversal_is_blocked(self):
        for path in ("/../app.py", "/../../etc/passwd", "/%2e%2e/app.py"):
            with self.subTest(path=path):
                response = raw_get("127.0.0.1", self.port, path)
                self.assertRegex(response.split("\r\n")[0], r"(403|404)")
                self.assertNotIn("API_URL", response)

    def test_unknown_api_route_returns_404(self):
        status, _headers, body = request(f"{self.base}/api/nope", payload={})
        self.assertEqual(status, 404)
        self.assertIn("Not found", body)

    # -- key verification ---------------------------------------------------

    def test_verify_accepts_a_working_key(self):
        status, _headers, body = request(f"{self.base}/api/verify", {"apiKey": KEY})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual([m["name"] for m in payload["models"]], ["jev-latest", "jev-preview"])

    def test_verify_reports_a_bad_key_without_leaking_it(self):
        status, _headers, body = request(f"{self.base}/api/verify", {"apiKey": "wrong-key"})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertIn("401", payload["error"])
        self.assertNotIn("wrong-key", body)

    def test_verify_requires_a_key(self):
        status, _headers, body = request(f"{self.base}/api/verify", {"apiKey": ""})
        self.assertEqual(status, 400)
        self.assertIn("API Key", body)

    # -- validation ---------------------------------------------------------

    def test_analyze_requires_a_key(self):
        status, _headers, body = request(
            f"{self.base}/api/analyze", {"apiKey": "", "rows": ["你好"]}
        )
        self.assertEqual(status, 400)
        self.assertIn("API Key", body)

    def test_analyze_rejects_blank_and_oversized_rows(self):
        status, _headers, body = request(
            f"{self.base}/api/analyze", {"apiKey": KEY, "rows": ["ok", "   "]}
        )
        self.assertEqual(status, 400)
        self.assertIn("空白", body)

        status, _headers, body = request(
            f"{self.base}/api/analyze", {"apiKey": KEY, "rows": ["x"] * 501}
        )
        self.assertEqual(status, 400)
        self.assertIn("500", body)

    def test_analyze_rejects_an_illegal_model_name(self):
        status, _headers, body = request(
            f"{self.base}/api/analyze",
            {"apiKey": KEY, "rows": ["你好"], "options": {"model": "bad model!"}},
        )
        self.assertEqual(status, 400)
        self.assertIn("模型名", body)

    # -- batch analysis -----------------------------------------------------

    def sample_rows(self):
        return [
            {"text": "怎么买？可以发链接吗", "source_index": 0},
            {"text": "看着不错，就是有点担心售后", "source_index": 1},
            {"text": "能不能出一期新手教程", "source_index": 2},
            {"text": "已经三天没发货了，客服也不回复", "source_index": 3},
            {"text": "哈哈哈这个演示太真实了", "source_index": 4},
        ]

    def test_analyze_returns_source_index_so_identity_columns_survive(self):
        status, _headers, body = request(
            f"{self.base}/api/analyze",
            {"apiKey": KEY, "rows": self.sample_rows(), "options": {"concurrency": 3}},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["count"], 5)
        self.assertEqual([item["source_index"] for item in payload["results"]], [0, 1, 2, 3, 4])
        self.assertEqual([item["index"] for item in payload["results"]], [0, 1, 2, 3, 4])

        by_index = {item["source_index"]: item for item in payload["results"]}
        self.assertEqual(by_index[0]["intent"], "purchase")
        self.assertEqual(by_index[1]["intent"], "objection")
        self.assertEqual(by_index[2]["intent"], "content_request")
        self.assertEqual(by_index[3]["intent"], "complaint")
        self.assertEqual(by_index[4]["intent"], "casual")
        self.assertEqual(by_index[0]["priority"], "高")
        self.assertEqual(by_index[4]["priority"], "低")
        self.assertEqual(by_index[0]["model"], "jev-1.13.0")

    def test_source_index_is_preserved_for_a_sparse_selection(self):
        rows = [
            {"text": "怎么买", "source_index": 11},
            {"text": "普通互动", "source_index": 42},
        ]
        _status, _headers, body = request(
            f"{self.base}/api/analyze", {"apiKey": KEY, "rows": rows}
        )
        result = json.loads(body)["results"]
        self.assertEqual([item["source_index"] for item in result], [11, 42])

    def test_legacy_string_rows_still_work(self):
        _status, _headers, body = request(
            f"{self.base}/api/analyze", {"apiKey": KEY, "rows": ["怎么买？链接呢"]}
        )
        result = json.loads(body)["results"]
        self.assertEqual(result[0]["intent"], "purchase")
        self.assertEqual(result[0]["source_index"], 0)

    def test_custom_thresholds_change_the_outcome(self):
        rows = [{"text": "怎么买？可以发链接吗", "source_index": 0}]
        _status, _headers, body = request(
            f"{self.base}/api/analyze",
            {
                "apiKey": KEY,
                "rows": rows,
                "options": {"thresholds": {"review_confidence": 0.95}},
            },
        )
        self.assertEqual(json.loads(body)["results"][0]["priority"], "人工复核")

        # Raising both high-priority gates demotes the purchase row to 中.
        _status, _headers, body = request(
            f"{self.base}/api/analyze",
            {
                "apiKey": KEY,
                "rows": rows,
                "options": {
                    "thresholds": {"high_purchase_score": 3.9, "high_urgent": 0.99}
                },
            },
        )
        self.assertEqual(json.loads(body)["results"][0]["priority"], "中")

    def test_low_confidence_row_is_flagged_for_review(self):
        _status, _headers, body = request(
            f"{self.base}/api/analyze",
            {"apiKey": KEY, "rows": [{"text": "怎么买 [unsure]", "source_index": 0}]},
        )
        item = json.loads(body)["results"][0]
        self.assertLess(item["intentConfidence"], 0.45)
        self.assertEqual(item["priority"], "人工复核")

    # -- failure handling ---------------------------------------------------

    def test_one_bad_row_does_not_kill_the_batch(self):
        rows = [
            {"text": "怎么买？可以发链接吗", "source_index": 0},
            {"text": "[reject] 这条会被拒绝", "source_index": 1},
            {"text": "哈哈哈", "source_index": 2},
        ]
        status, _headers, body = request(
            f"{self.base}/api/analyze", {"apiKey": KEY, "rows": rows}
        )
        self.assertEqual(status, 200)
        results = json.loads(body)["results"]
        self.assertEqual(len(results), 3)
        self.assertEqual(results[1]["priority"], "失败")
        self.assertIn("422", results[1]["error"])
        self.assertNotIn("error", results[0])
        self.assertNotIn("error", results[2])

    def test_transient_overload_is_retried_until_it_succeeds(self):
        _status, _headers, body = request(
            f"{self.base}/api/analyze",
            {"apiKey": KEY, "rows": [{"text": "[retry] 怎么买", "source_index": 0}]},
        )
        item = json.loads(body)["results"][0]
        self.assertNotIn("error", item)
        self.assertEqual(item["intent"], "purchase")

    # -- streaming ----------------------------------------------------------

    def test_stream_emits_start_results_and_done(self):
        status, headers, body = request(
            f"{self.base}/api/analyze",
            {"apiKey": KEY, "rows": self.sample_rows(), "stream": True},
        )
        self.assertEqual(status, 200)
        self.assertIn("ndjson", headers.get("Content-Type", ""))

        events = [json.loads(line) for line in body.splitlines() if line.strip()]
        self.assertEqual(events[0]["type"], "start")
        self.assertEqual(events[0]["total"], 5)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["count"], 5)
        self.assertEqual(events[-1]["failed"], 0)

        results = [event["result"] for event in events if event["type"] == "result"]
        self.assertEqual(len(results), 5)
        self.assertEqual(sorted(item["source_index"] for item in results), [0, 1, 2, 3, 4])
        for item in results:
            self.assertIn("priority", item)
            self.assertIn("text", item)

    def test_stream_reports_failures_in_the_done_event(self):
        rows = [
            {"text": "[reject] 坏行", "source_index": 0},
            {"text": "哈哈哈", "source_index": 1},
        ]
        _status, _headers, body = request(
            f"{self.base}/api/analyze", {"apiKey": KEY, "rows": rows, "stream": True}
        )
        events = [json.loads(line) for line in body.splitlines() if line.strip()]
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["failed"], 1)

    def test_stream_still_validates_before_streaming(self):
        status, _headers, body = request(
            f"{self.base}/api/analyze", {"apiKey": "", "rows": ["你好"], "stream": True}
        )
        self.assertEqual(status, 400)
        self.assertIn("API Key", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
