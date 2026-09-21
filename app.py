#!/usr/bin/env python3
"""Local server for Comment Opportunity Radar.

No third-party packages are required. The browser talks only to this local
server; the server forwards evaluation requests to TypeSafe's official API.

Environment variables:
    RADAR_HOST        bind address (default 127.0.0.1)
    RADAR_PORT        bind port (default 8765)
    RADAR_API_BASE    override the TypeSafe base URL (used by the test suite)
    RADAR_MODEL       default model id (default jev-1.13.0, pinned on purpose)
    RADAR_PROXY       proxy URL for outbound calls; "direct" forces no proxy
"""

from __future__ import annotations

import json
import mimetypes
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"

API_BASE = os.environ.get("RADAR_API_BASE", "https://api.typesafe.ai").rstrip("/")
EVAL_URL = f"{API_BASE}/v1/systemone"
MODELS_URL = f"{API_BASE}/v1/models"
USER_AGENT = "comment-opportunity-radar/0.2"

MAX_ROWS = 500
MAX_TEXT_CHARS = 8000
MAX_BODY_BYTES = 4_000_000
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 8
REQUEST_TIMEOUT = 60
MAX_ATTEMPTS = 4

# Pinned on purpose. The official docs are explicit: an alias moves when a new
# release ships, so once you have tuned confidence thresholds against a version
# you should pin that version instead of the alias. Set RADAR_MODEL=jev-latest
# if you would rather always track the newest release.
DEFAULT_MODEL = os.environ.get("RADAR_MODEL", "jev-1.13.0").strip() or "jev-1.13.0"
KNOWN_ALIASES = ("jev-latest", "jev-preview")

# Every threshold is a plain number the caller may override per request, so the
# values can be calibrated against real data instead of being frozen in code.
THRESHOLD_BOUNDS: dict[str, tuple[float, float]] = {
    "review_confidence": (0.0, 1.0),
    "high_purchase_score": (0.0, 4.0),
    "high_urgent": (0.0, 1.0),
    "medium_purchase_score": (0.0, 4.0),
    "medium_urgent": (0.0, 1.0),
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    # Below this Choice confidence the row is handed to a human. 0.45 on a
    # 5-option question means the top option needs roughly a 56% probability,
    # because Jev normalises confidence by the number of options.
    "review_confidence": 0.45,
    "high_purchase_score": 2.5,
    "high_urgent": 0.65,
    "medium_purchase_score": 1.5,
    "medium_urgent": 0.45,
}

MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

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

# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _build_opener() -> urllib.request.OpenerDirector:
    """Build an opener that respects RADAR_PROXY, then env/system proxies."""
    explicit = os.environ.get("RADAR_PROXY", "").strip()
    if explicit.lower() in {"direct", "none", "off"}:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if explicit:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": explicit, "https": explicit})
        )
    # No explicit setting: fall back to the standard proxy environment
    # variables and the macOS system settings.
    return urllib.request.build_opener()


OPENER = _build_opener()


def proxy_in_use() -> str:
    explicit = os.environ.get("RADAR_PROXY", "").strip()
    if explicit.lower() in {"direct", "none", "off"}:
        return ""
    if explicit:
        return explicit
    proxies = urllib.request.getproxies()
    return proxies.get("https") or proxies.get("http") or ""


def _retryable_status(status: int) -> bool:
    return status in {429, 529} or 500 <= status < 600


def _backoff_seconds(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return max(0.0, min(30.0, float(retry_after.strip())))
        except (TypeError, ValueError):
            pass
    return min(30.0, 2**attempt) + random.uniform(0.0, 0.4)


def _error_message(body: str) -> str:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()[:600] or "（无响应内容）"
    if isinstance(parsed, dict):
        detail = parsed.get("detail") or parsed.get("error") or parsed.get("message")
        if isinstance(detail, list):
            return json.dumps(detail, ensure_ascii=False)[:600]
        if detail:
            return str(detail)[:600]
    return body.strip()[:600]


def call_typesafe(api_key: str, text: str, model: str) -> dict[str, Any]:
    """POST one comment to the evaluation endpoint, retrying transient errors."""
    payload = json.dumps(
        {"state": text, "model": model, "questions": QUESTIONS},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        EVAL_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            # 401/403/422 are the caller's problem: retrying cannot help and
            # would just burn time.
            if not _retryable_status(error.code) or attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(
                    f"TypeSafe API 返回 {error.code}：{_error_message(body)}"
                ) from error
            last_error = error
            time.sleep(_backoff_seconds(attempt, error.headers.get("Retry-After")))
        except urllib.error.URLError as error:
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(
                    f"无法连接 TypeSafe API：{error.reason}"
                ) from error
            last_error = error
            time.sleep(_backoff_seconds(attempt, None))

    raise RuntimeError(f"TypeSafe API 请求失败：{last_error}")


def fetch_models(api_key: str) -> list[dict[str, Any]]:
    """GET /v1/models — a cheap way to prove the key works before spending quota."""
    request = urllib.request.Request(
        MODELS_URL,
        method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with OPENER.open(request, timeout=30) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"TypeSafe API 返回 {error.code}：{_error_message(body)}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"无法连接 TypeSafe API：{error.reason}") from error

    models = parsed.get("models") if isinstance(parsed, dict) else parsed
    return models if isinstance(models, list) else []


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def clamp_probability(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def resolve_thresholds(raw: Any) -> dict[str, float]:
    """Merge caller-supplied thresholds over the defaults, ignoring junk."""
    resolved = dict(DEFAULT_THRESHOLDS)
    if not isinstance(raw, dict):
        return resolved
    for key, (low, high) in THRESHOLD_BOUNDS.items():
        if key not in raw:
            continue
        try:
            value = float(raw[key])
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN never compares equal to itself
            continue
        resolved[key] = round(max(low, min(high, value)), 4)
    return resolved


def resolve_options(raw: Any) -> dict[str, Any]:
    """Validate the per-request options block."""
    raw = raw if isinstance(raw, dict) else {}

    model = str(raw.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if not MODEL_PATTERN.match(model):
        raise ValueError("模型名不合法")

    try:
        concurrency = int(raw.get("concurrency", DEFAULT_CONCURRENCY))
    except (TypeError, ValueError):
        concurrency = DEFAULT_CONCURRENCY
    concurrency = max(1, min(MAX_CONCURRENCY, concurrency))

    return {
        "model": model,
        "concurrency": concurrency,
        "thresholds": resolve_thresholds(raw.get("thresholds")),
    }


def priority_for(
    intent: str,
    purchase_score: float,
    urgent: float,
    confidence: float,
    thresholds: dict[str, float] | None = None,
) -> str:
    limits = resolve_thresholds(thresholds)
    if confidence < limits["review_confidence"]:
        return "人工复核"
    if intent in {"purchase", "complaint"} and (
        purchase_score >= limits["high_purchase_score"]
        or urgent >= limits["high_urgent"]
    ):
        return "高"
    if (
        intent in {"objection", "content_request"}
        or purchase_score >= limits["medium_purchase_score"]
        or urgent >= limits["medium_urgent"]
    ):
        return "中"
    return "低"


def normalize_rows(raw: Any) -> list[dict[str, Any]]:
    """Accept both the current object form and the legacy list-of-strings form."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("没有可分析的评论")
    if len(raw) > MAX_ROWS:
        raise ValueError(f"单次最多处理 {MAX_ROWS} 条评论")

    rows: list[dict[str, Any]] = []
    for position, item in enumerate(raw):
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            raw_source = item.get("source_index", item.get("sourceIndex", position))
            try:
                source_index = int(raw_source)
            except (TypeError, ValueError):
                source_index = position
        else:
            text = str(item).strip()
            source_index = position
        rows.append(
            {
                "index": position,
                "source_index": source_index,
                "text": text[:MAX_TEXT_CHARS],
            }
        )

    if any(not row["text"] for row in rows):
        raise ValueError("评论中包含空白内容")
    return rows


def normalize_result(
    row: dict[str, Any],
    response: dict[str, Any],
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    answers = response.get("answers") or {}
    intent_answer = answers.get("intent") or {}
    score_answer = answers.get("purchase_intent") or {}
    urgent_answer = answers.get("needs_fast_reply") or {}

    intent = str(intent_answer.get("choice", "unknown"))
    confidence = clamp_probability(intent_answer.get("confidence"))
    try:
        score = float(score_answer.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    urgent = clamp_probability(urgent_answer.get("noul"))
    probabilities = intent_answer.get("probabilities")

    return {
        "index": row["index"],
        "source_index": row["source_index"],
        "text": row["text"],
        "intent": intent,
        "intentConfidence": confidence,
        "purchaseScore": score,
        "urgentProbability": urgent,
        "priority": priority_for(intent, score, urgent, confidence, thresholds),
        "probabilities": probabilities if isinstance(probabilities, dict) else {},
        "model": response.get("model") or "",
        "usage": response.get("usage") or {},
    }


def analyze_rows(
    api_key: str,
    rows: list[dict[str, Any]],
    options: dict[str, Any],
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    lock = threading.Lock()

    def record(item: dict[str, Any]) -> None:
        with lock:
            results.append(item)
        if on_result is not None:
            on_result(item)

    with ThreadPoolExecutor(max_workers=options["concurrency"]) as pool:
        futures = {
            pool.submit(call_typesafe, api_key, row["text"], options["model"]): row
            for row in rows
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                record(normalize_result(row, future.result(), options["thresholds"]))
            except Exception as error:  # keep the other rows usable
                record(
                    {
                        "index": row["index"],
                        "source_index": row["source_index"],
                        "text": row["text"],
                        "error": str(error),
                        "priority": "失败",
                    }
                )

    return sorted(results, key=lambda item: item["index"])


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "OpportunityRadar/0.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def read_payload(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Content-Length 不合法") from error
        if length <= 0:
            raise ValueError("请求内容为空")
        if length > MAX_BODY_BYTES:
            raise ValueError("请求内容过大")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/analyze":
            self.handle_analyze()
        elif self.path == "/api/verify":
            self.handle_verify()
        else:
            self.send_json(404, {"error": "Not found"})

    def handle_analyze(self) -> None:
        try:
            payload = self.read_payload()
            api_key = str(payload.get("apiKey", "")).strip()
            if not api_key:
                raise ValueError("请填写 TypeSafe API Key")
            rows = normalize_rows(payload.get("rows"))
            options = resolve_options(payload.get("options"))
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
            return

        streaming = bool(payload.get("stream"))
        try:
            if streaming:
                self.stream_analyze(api_key, rows, options)
            else:
                results = analyze_rows(api_key, rows, options)
                self.send_json(200, {"results": results, "count": len(results)})
        except Exception as error:  # noqa: BLE001 - surfaced to the browser
            if streaming:
                return  # headers already sent; the stream just ends
            self.send_json(500, {"error": str(error)})

    def stream_analyze(
        self,
        api_key: str,
        rows: list[dict[str, Any]],
        options: dict[str, Any],
    ) -> None:
        """Emit one NDJSON line per finished row so the UI can show progress."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self._common_headers()
        self.end_headers()
        self.close_connection = True

        def write_line(value: dict[str, Any]) -> None:
            self.wfile.write(
                (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
            )
            self.wfile.flush()

        write_line({"type": "start", "total": len(rows), "model": options["model"]})
        try:
            results = analyze_rows(
                api_key, rows, options, on_result=lambda item: write_line(
                    {"type": "result", "result": item}
                )
            )
            write_line(
                {
                    "type": "done",
                    "count": len(results),
                    "failed": sum(1 for item in results if item.get("error")),
                }
            )
        except Exception as error:  # noqa: BLE001
            write_line({"type": "error", "error": str(error)})

    def handle_verify(self) -> None:
        try:
            payload = self.read_payload()
            api_key = str(payload.get("apiKey", "")).strip()
            if not api_key:
                raise ValueError("请填写 TypeSafe API Key")
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
            return
        try:
            models = fetch_models(api_key)
        except Exception as error:  # noqa: BLE001
            self.send_json(200, {"ok": False, "error": str(error)})
            return
        self.send_json(200, {"ok": True, "models": models})

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/config":
            self.send_json(
                200,
                {
                    "model": DEFAULT_MODEL,
                    "knownAliases": list(KNOWN_ALIASES),
                    "thresholds": DEFAULT_THRESHOLDS,
                    "thresholdBounds": {
                        key: list(bounds) for key, bounds in THRESHOLD_BOUNDS.items()
                    },
                    "maxRows": MAX_ROWS,
                    "concurrency": DEFAULT_CONCURRENCY,
                    "maxConcurrency": MAX_CONCURRENCY,
                    "apiBase": API_BASE,
                    "proxy": proxy_in_use(),
                },
            )
            return

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
        self.send_header(
            "Content-Type", f"{content_type or 'application/octet-stream'}; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(content)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(content)


def main() -> None:
    host = os.environ.get("RADAR_HOST", "127.0.0.1")
    port = int(os.environ.get("RADAR_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"评论商机雷达已启动：http://{host}:{port}")
    print(f"模型：{DEFAULT_MODEL}    上游：{API_BASE}")
    if proxy_in_use():
        print(f"出站代理：{proxy_in_use()}")
    print("按 Ctrl+C 停止。API Key 不会写入磁盘。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
