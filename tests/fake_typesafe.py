#!/usr/bin/env python3
"""A stand-in for the TypeSafe evaluation API, used by the test suite.

It mirrors the response shape documented at https://docs.typesafe.ai/api so the
radar can be exercised end to end without a real API key and without spend.

Fault injection, driven by markers inside the comment text:
    [unsure]   -> very flat probability distribution (low confidence)
    [reject]   -> always 422, so the row is reported as failed
    [fail]     -> always 503, exercises the retry-exhaustion path
    [retry]    -> 529 twice, then succeeds, so the backoff path is exercised
    [slow]     -> sleeps briefly, so streaming progress can be observed

Usage: python3 fake_typesafe.py <port>
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VALID_KEY = "test-key"

OPTIONS = ["purchase", "objection", "content_request", "complaint", "casual"]

RULES = [
    ("purchase", r"怎么买|链接|价格|下单|多少钱|库存|购买|优惠券|优惠"),
    ("objection", r"担心|售后|靠谱|真的假的|会不会|效果|不适合"),
    ("content_request", r"教程|出一期|测评|讲讲|介绍一下|有没有视频"),
    ("complaint", r"没发货|投诉|退款|客服|物流|骗"),
]

COUNTS: dict[str, int] = {}
LOCK = threading.Lock()


def bump(key: str) -> int:
    with LOCK:
        COUNTS[key] = COUNTS.get(key, 0) + 1
        return COUNTS[key]


def classify(text: str) -> tuple[str, float, float, float]:
    for intent, pattern in RULES:
        if re.search(pattern, text):
            break
    else:
        intent = "casual"

    table = {
        "purchase": (3.6, 0.86, 0.87),
        "objection": (2.1, 0.52, 0.78),
        "content_request": (1.3, 0.24, 0.81),
        "complaint": (0.6, 0.91, 0.83),
        "casual": (0.1, 0.07, 0.88),
    }
    score, urgent, confidence = table[intent]

    if "[unsure]" in text:
        # Nearly uniform across all five options -> confidence collapses.
        confidence = 0.22

    return intent, score, urgent, confidence


def probabilities(intent: str, confidence: float) -> dict[str, float]:
    if confidence < 0.3:
        values = {option: 0.2 for option in OPTIONS}
    else:
        remaining = (1 - confidence) / (len(OPTIONS) - 1)
        values = {option: remaining for option in OPTIONS}
        values[intent] = confidence
    total = sum(values.values())
    return {key: round(value / total, 4) for key, value in values.items()}


def build_response(state: str, model: str) -> dict:
    text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    intent, score, urgent, confidence = classify(text)
    return {
        "model": model,
        "answers": {
            "intent": {
                "type": "choice",
                "choice": intent,
                "probabilities": probabilities(intent, confidence),
                "confidence": confidence,
            },
            "purchase_intent": {
                "type": "score",
                "score": score,
                "legend": {str(i): f"level-{i}" for i in range(5)},
                "probabilities": {"0": 0.05, "1": 0.05, "2": 0.1, "3": 0.4, "4": 0.4},
                "confidence": 0.7,
            },
            "needs_fast_reply": {"type": "noul", "noul": urgent},
        },
        "usage": {"input_tokens": 320, "output_tokens": 28},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeTypeSafe/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the test output clean
        pass

    def _json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {VALID_KEY}"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/__ping":
            self._json(200, {"ok": True, "counts": dict(COUNTS)})
            return
        if self.path == "/v1/models":
            if not self._authorized():
                self._json(401, {"detail": "Missing or invalid API key"})
                return
            self._json(
                200,
                {
                    "models": [
                        {"name": "jev-latest", "description": "Most recent stable", "release_date": "2026-08-01"},
                        {"name": "jev-preview", "description": "Most recent build", "release_date": "2026-08-01"},
                    ]
                },
            )
            return
        self._json(404, {"detail": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/systemone":
            self._json(404, {"detail": "Not found"})
            return
        if not self._authorized():
            self._json(401, {"detail": "Missing or invalid API key"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(422, {"detail": "Malformed JSON body"})
            return

        model = payload.get("model")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model):
            self._json(422, {"detail": "model: must be a valid model id"})
            return
        questions = payload.get("questions")
        if not isinstance(questions, dict) or "intent" not in questions:
            self._json(422, {"detail": "questions.intent: field required"})
            return

        text = payload.get("state")
        if not isinstance(text, str):
            self._json(422, {"detail": "state: must be a string"})
            return

        if "[reject]" in text:
            self._json(422, {"detail": "state: rejected by the evaluation model"})
            return
        if "[fail]" in text:
            self._json(503, {"detail": "Service unavailable"})
            return
        if "[retry]" in text and bump("retry") <= 2:
            self._json(529, {"detail": "Overloaded"})
            return
        if "[slow]" in text:
            time.sleep(0.35)

        self._json(200, build_response(text, model))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
