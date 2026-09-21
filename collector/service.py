"""A single-threaded owner for the browser, callable from the HTTP layer.

Playwright's sync API must be used from the thread that created it, and the
logged-in browser has to survive across several HTTP requests (login, then list
works, then scrape comments). Both constraints are satisfied by parking the
browser on a dedicated worker thread and feeding it jobs through a queue.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import douyin

KNOWN_OPERATIONS = frozenset(
    {"status", "login_open", "login_wait", "works", "collect", "doctor", "shutdown"}
)


@dataclass
class ServiceStatus:
    running: bool = False
    logged_in: bool = False
    nickname: str = ""
    profile: str = ""
    last_error: str = ""
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "loggedIn": self.logged_in,
            "nickname": self.nickname,
            "profile": self.profile,
            "lastError": self.last_error,
        }


class CollectorService:
    """Serialises every browser interaction onto one worker thread."""

    def __init__(
        self,
        profile_dir: Path | str = douyin.DEFAULT_PROFILE_DIR,
        channel: str = douyin.DEFAULT_CHANNEL,
        headless: bool = False,
    ) -> None:
        self._profile = Path(profile_dir)
        self._channel = channel
        self._headless = headless
        self._collector: douyin.DouyinCollector | None = None
        self._jobs: queue.Queue[Any] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status = ServiceStatus(profile=str(self._profile))

    # -- plumbing ----------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._loop, name="radar-collector", daemon=True
            )
            self._worker.start()

    def _loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                break
            op, params, box, progress = job
            try:
                box["result"] = self._dispatch(op, params, progress)
            except Exception as exc:  # surfaced to the caller
                box["error"] = exc
            finally:
                self._status.updated_at = time.time()
                box["done"].set()

    def _dispatch(self, op: str, params: dict[str, Any], progress: Callable | None):
        # Validate the operation before touching the browser: an unknown op must
        # not have the side effect of launching Chrome.
        if op not in KNOWN_OPERATIONS:
            raise ValueError(f"未知操作：{op}")
        if op == "status":
            return self._read_status(refresh=params.get("refresh", False))

        collector = self._ensure_collector()
        if op == "login_open":
            title = collector.open_login()
            self._read_status(refresh=False)
            return {"title": title, "loggedIn": collector.logged_in()}
        if op == "login_wait":
            ok = collector.wait_for_login(timeout=float(params.get("timeout", 240.0)))
            self._read_status(refresh=True)
            return {"loggedIn": ok, "nickname": self._status.nickname}
        if op == "works":
            works = collector.list_works(limit=int(params.get("limit", 60)))
            self._read_status(refresh=False)
            return [self._work_dict(w) for w in works]
        if op == "collect":
            return self._collect(collector, params, progress)
        if op == "doctor":
            return self._doctor(collector)
        if op == "shutdown":
            collector.stop()
            self._status.running = False
            self._status.logged_in = False
            self._status.nickname = ""
            return {"ok": True}
        raise ValueError(f"未知操作：{op}")  # pragma: no cover - guarded above

    def _ensure_collector(self) -> douyin.DouyinCollector:
        if self._collector is None:
            self._collector = douyin.DouyinCollector(
                profile_dir=self._profile, channel=self._channel, headless=self._headless
            )
        if not self._collector.running:
            self._collector.start()
            self._status.running = True
        return self._collector

    def call(self, op: str, on_progress: Callable | None = None,
             timeout: float | None = 900.0, **params: Any):
        """Run one job on the worker thread and block for its result."""
        self._ensure_worker()
        box: dict[str, Any] = {"done": threading.Event()}
        self._jobs.put((op, params, box, on_progress))
        if not box["done"].wait(timeout):
            raise douyin.CollectorError(f"操作 {op} 超时（{timeout}s）")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def submit(self, op: str, on_progress: Callable | None = None, **params: Any):
        """Fire-and-forget: used by the UI's login button."""
        self._ensure_worker()
        box: dict[str, Any] = {"done": threading.Event()}
        self._jobs.put((op, params, box, on_progress))
        return box

    # -- operations --------------------------------------------------------

    def status(self, refresh: bool = False) -> dict[str, Any]:
        try:
            return self.call("status", refresh=refresh, timeout=30)
        except Exception as exc:
            self._status.last_error = str(exc)
            return self._status.as_dict()

    def _read_status(self, refresh: bool) -> dict[str, Any]:
        collector = self._collector
        if collector is None or not collector.running:
            self._status.running = False
            self._status.logged_in = False
            return self._status.as_dict()
        self._status.running = True
        self._status.logged_in = collector.logged_in()
        if refresh and self._status.logged_in:
            self._status.nickname = collector.whoami()
        elif not self._status.logged_in:
            self._status.nickname = ""
        self._status.last_error = ""
        self._status.updated_at = time.time()
        return self._status.as_dict()

    def _collect(self, collector: douyin.DouyinCollector, params: dict[str, Any],
                 progress: Callable | None) -> dict[str, Any]:
        works = params.get("works") or []
        works_by_id = {
            str(w.get("aweme_id")): douyin.Work(
                aweme_id=str(w.get("aweme_id")),
                desc=str(w.get("desc") or ""),
                comment_count=int(w.get("commentCount") or 0),
            )
            for w in works
            if w.get("aweme_id")
        }

        all_comments: list[douyin.Comment] = []
        errors: list[dict[str, str]] = []
        for index, aweme_id in enumerate(params["awemeIds"]):
            if progress is not None:
                progress({"type": "work", "index": index, "awemeId": aweme_id})
            known = works_by_id.get(aweme_id)
            known_title = (known.desc if known else "") or ""

            def on_progress(count: int, title: str, _known: str = known_title) -> None:
                # Prefer the title we already have from the works list. Reading
                # it off the page lags on this SPA, so the first ticks of every
                # work would show the *previous* video's name.
                if progress is not None:
                    progress({"type": "progress", "count": count, "title": _known or title})
            try:
                # maxScrolls is only honoured when explicitly pinned; otherwise
                # the collector derives a budget from maxComments, which is what
                # makes the run stop as soon as it has what it came for.
                pins = {}
                if params.get("maxScrolls") not in (None, ""):
                    pins["max_scrolls"] = int(params["maxScrolls"])
                comments = collector.collect_comments(
                    aweme_id,
                    max_comments=int(params.get("maxComments", 500)),
                    include_replies=bool(params.get("includeReplies", False)),
                    on_progress=on_progress,
                    **pins,
                )
                all_comments.extend(comments)
            except douyin.CollectorError as exc:
                errors.append({"awemeId": aweme_id, "error": str(exc)})
                if progress is not None:
                    progress({"type": "error", "awemeId": aweme_id, "error": str(exc)})
                continue

        deduped = douyin.dedupe_comments(all_comments)
        analyzable, skipped_no_text = douyin.split_analyzable(deduped)
        rows = douyin.comment_rows(analyzable, works_by_id)
        if progress is not None and skipped_no_text:
            progress({"type": "note", "skippedNoText": skipped_no_text})
        return {
            "count": len(analyzable),
            "skippedNoText": skipped_no_text,
            "header": douyin.CSV_HEADER,
            "rows": rows,
            "csv": douyin.to_csv_text(rows, douyin.CSV_HEADER),
            "errors": errors,
        }

    def _doctor(self, collector: douyin.DouyinCollector) -> dict[str, Any]:
        report: dict[str, Any] = {
            "profile": str(self._profile),
            "channel": self._channel,
            "loggedIn": collector.logged_in(),
            "nickname": "",
            "anonymousVideoSample": "",
            "anonymousComments": 0,
            "notes": [],
        }
        if collector.logged_in():
            report["nickname"] = collector.whoami()
        else:
            report["notes"].append("未登录：只能验证匿名抓取通路，作品列表不可用。")
        try:
            ids = collector.hot_video_ids(limit=1)
            if not ids:
                report["notes"].append("热点榜没有取到视频 ID，可能遇到风控。")
                return report
            report["anonymousVideoSample"] = ids[0]
            comments = collector.collect_comments(ids[0], max_comments=30, max_scrolls=8)
            report["anonymousComments"] = len(comments)
            if not comments:
                report["notes"].append("视频页没有抓到评论，可能遇到风控或该作品无评论。")
        except douyin.CollectorError as exc:
            report["notes"].append(f"抓取失败：{exc}")
        return report

    def shutdown(self) -> None:
        try:
            if self._worker is not None and self._worker.is_alive():
                self.call("shutdown", timeout=60)
        except Exception:
            pass
        if self._collector is not None:
            try:
                self._collector.stop()
            except Exception:
                pass
        self._jobs.put(None)

    @staticmethod
    def _work_dict(work: douyin.Work) -> dict[str, Any]:
        return {
            "awemeId": work.aweme_id,
            "title": work.title,
            "desc": work.desc,
            "commentCount": work.comment_count,
            "diggCount": work.digg_count,
            "createTime": work.create_time,
            "createdAt": douyin.format_time(work.create_time),
            "url": work.url,
        }


_SERVICE: CollectorService | None = None
_SERVICE_LOCK = threading.Lock()


def get_service() -> CollectorService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = CollectorService()
        return _SERVICE
