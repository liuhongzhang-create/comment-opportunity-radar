#!/usr/bin/env python3
"""Local server for Comment Opportunity Radar.

No third-party packages are required. The browser talks only to this local
server; the server forwards evaluation requests to TypeSafe's official API.
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
API_URL = "https://api.typesafe.ai/v1/systemone"
MAX_ROWS = 500
MAX_WORKERS = 4

QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "判断这条评论最主要的商业意图。只依据评论本身，不要臆测用户未表达的信息。",
        "criteria": {
            "purchase": "明确询问购买、价格、链接、库存、下单方式或表达购买意愿",
            "objection": "对价格、效果、可信度、适用性等存在成交阻碍或顾虑",
            "content_request": "提出教程、测评、选题、功能介绍等内容需求",
            "complaint": "投诉商品、物流、客服、退款或使用体验",
            "casual": "普通互动、表情、玩笑、无明确商业意图或无法归类",
        },
    },
    "purchase_intent": {
        "type": "score",
        "instructions": "评估评论者当前表现出的购买意向强度。没有购买信号时应选择最低等级。",
        "criteria": [
            "没有购买意向",
            "轻微兴趣，但没有询问购买信息",
            "正在了解价格、功能或适用性",
            "明显考虑购买，正在解决最后顾虑",
            "明确准备下单或要求购买入口",
        ],
    },
    "needs_fast_reply": {
        "type": "noul",
        "instructions": "这条评论是否值得商家尽快人工回复，以促成交易、避免客户流失或控制投诉升级？",
        "criteria": {
            "true": "及时回复可能明显影响成交、留存或风险控制",
            "false": "无需优先人工处理",
        },
    },
}


def clamp_probability(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def priority_for(intent: str, purchase_score: float, urgent: float, confidence: float) -> str:
    if confidence < 0.45:
        return "人工复核"
    if intent in {"purchase", "complaint"} and (purchase_score >= 2.5 or urgent >= 0.65):
        return "高"
    if intent in {"objection", "content_request"} or purchase_score >= 1.5 or urgent >= 0.45:
        return "中"
    return "低"


def call_typesafe(api_key: str, text: str) -> dict[str, Any]:
    payload = json.dumps(
        {"state": text, "model": "jev-latest", "questions": QUESTIONS},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "comment-opportunity-radar/0.1",
        },
    )

    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if error.code in {429, 529} and attempt < 3:
                time.sleep(2**attempt)
                continue
            message = body
            try:
                parsed = json.loads(body)
                message = parsed.get("detail") or parsed.get("message") or body
            except json.JSONDecodeError:
                pass
            raise RuntimeError(f"TypeSafe API 返回 {error.code}：{message}") from error
        except urllib.error.URLError as error:
            if attempt < 3:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"无法连接 TypeSafe API：{error.reason}") from error

    raise RuntimeError("TypeSafe API 请求失败")


def normalize_result(index: int, text: str, response: dict[str, Any]) -> dict[str, Any]:
    answers = response.get("answers", {})
    intent_answer = answers.get("intent", {})
    score_answer = answers.get("purchase_intent", {})
    urgent_answer = answers.get("needs_fast_reply", {})

    intent = str(intent_answer.get("choice", "unknown"))
    confidence = clamp_probability(intent_answer.get("confidence"))
    score = float(score_answer.get("score", 0.0) or 0.0)
    urgent = clamp_probability(urgent_answer.get("noul"))
    probabilities = intent_answer.get("probabilities", {})

    return {
        "index": index,
        "text": text,
        "intent": intent,
        "intentConfidence": confidence,
        "purchaseScore": score,
        "urgentProbability": urgent,
        "priority": priority_for(intent, score, urgent, confidence),
        "probabilities": probabilities,
        "model": response.get("model", "jev-latest"),
        "usage": response.get("usage", {}),
    }


def analyze_rows(api_key: str, rows: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(call_typesafe, api_key, text): (index, text)
            for index, text in enumerate(rows)
        }
        for future in as_completed(futures):
            index, text = futures[future]
            try:
                results.append(normalize_result(index, text, future.result()))
            except Exception as error:  # keep other rows usable
                results.append(
                    {
                        "index": index,
                        "text": text,
                        "error": str(error),
                        "priority": "失败",
                    }
                )
    return sorted(results, key=lambda item: item["index"])


class Handler(BaseHTTPRequestHandler):
    server_version = "OpportunityRadar/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/analyze":
            self.send_json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise ValueError("请求内容为空或过大")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            api_key = str(payload.get("apiKey", "")).strip()
            rows = payload.get("rows")
            if not api_key:
                raise ValueError("请填写 TypeSafe API Key")
            if not isinstance(rows, list) or not rows:
                raise ValueError("没有可分析的评论")
            if len(rows) > MAX_ROWS:
                raise ValueError(f"单次最多处理 {MAX_ROWS} 条评论")
            cleaned = [str(item).strip()[:8000] for item in rows]
            if any(not text for text in cleaned):
                raise ValueError("评论中包含空白内容")
            results = analyze_rows(api_key, cleaned)
            self.send_json(200, {"results": results, "count": len(results)})
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
        except Exception as error:
            self.send_json(500, {"error": str(error)})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if WEB_ROOT not in target.parents and target != WEB_ROOT:
            self.send_error(403)
            return
        if not target.is_file():
            self.send_error(404)
            return
        content = target.read_bytes()
        content_type, _ = mimetypes.guess_type(str(target))
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> None:
    host = os.environ.get("RADAR_HOST", "127.0.0.1")
    port = int(os.environ.get("RADAR_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"评论商机雷达已启动：http://{host}:{port}")
    print("按 Ctrl+C 停止。API Key 不会写入磁盘。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
