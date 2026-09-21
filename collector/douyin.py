"""Drive a real, logged-in Chrome to read your own Douyin works and comments.

Why a browser instead of plain HTTP
-----------------------------------
Douyin signs every `aweme/v1/web/*` call with a token that only its own page
JavaScript can produce, and the raw endpoints answer signature-less requests
with an empty body. The site also shows a captcha wall to browsers that look
automated. So the only workable approach is the one this module takes: start a
real Chrome with a persistent profile, let the *page itself* sign and issue its
own requests, and harvest the JSON it already received. Nothing is forged and
no request is sent that the page would not have sent on its own while you
scrolled.

Compliance
----------
This automates a site on your behalf with your own account and your own data.
That is a grey area in Douyin's terms of service, and heavy use can trip risk
controls. Keep the pace human, only collect works you own, and prefer the
official creator tooling when it can answer the same question.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

HOME_URL = "https://www.douyin.com"
HOT_URL = f"{HOME_URL}/hot"
SELF_POSTS_URL = f"{HOME_URL}/user/self?showTab=post"
VIDEO_URL = f"{HOME_URL}/video/{{aweme_id}}"

COMMENT_API = "/aweme/v1/web/comment/list/"
REPLY_API = "/aweme/v1/web/comment/list/reply/"
POSTS_API = "/aweme/v1/web/aweme/post/"

SESSION_COOKIES = ("sessionid", "sessionid_ss", "sid_tt")

DEFAULT_PROFILE_DIR = Path(
    os.environ.get("RADAR_BROWSER_PROFILE", str(Path.home() / ".radar-douyin-profile"))
)
DEFAULT_CHANNEL = os.environ.get("RADAR_BROWSER_CHANNEL", "chrome").strip() or "chrome"

# One column in the collected CSV must be recognisable as the comment text by
# the analyser (it looks for a header containing 评论). Keep this the only one.
CSV_HEADER = [
    "用户ID",
    "昵称",
    "主页链接",
    "评论内容",
    "点赞数",
    "发布时间",
    "IP归属",
    "作品标题",
    "作品链接",
    "cid",
]

_LOGIN_HINT = re.compile(r"扫码登录|立即登录|登录后查看|手机号登录")

# Risk-control browsers get served this instead of the site.
_BLOCK_MARKERS = ("验证码中间页", "验证码", "安全验证")


# ---------------------------------------------------------------------------
# Pure helpers — no Playwright, no network, fully unit-testable
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Comment:
    cid: str
    text: str
    aweme_id: str = ""
    nickname: str = ""
    uid: str = ""
    sec_uid: str = ""
    unique_id: str = ""
    digg_count: int = 0
    create_time: int = 0
    ip_label: str = ""
    reply_total: int = 0
    level: int = 1
    parent_cid: str = ""
    source: str = "top"

    @property
    def profile_url(self) -> str:
        return f"{HOME_URL}/user/{self.sec_uid}" if self.sec_uid else ""

    @property
    def identity(self) -> str:
        """Best available stable identifier for the commenter."""
        return self.sec_uid or self.uid or self.unique_id or self.nickname


@dataclass(slots=True)
class Work:
    aweme_id: str
    desc: str = ""
    create_time: int = 0
    comment_count: int = 0
    digg_count: int = 0
    share_url: str = ""
    collected: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        first = (self.desc or "").strip().splitlines()
        return (first[0] if first else "")[:60] or f"作品 {self.aweme_id}"

    @property
    def url(self) -> str:
        return VIDEO_URL.format(aweme_id=self.aweme_id)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clean_text(value: Any) -> str:
    """Collapse whitespace but keep emoji and punctuation intact.

    Douyin comments carry emoji as real characters; they are signal, so they are
    preserved. Only control characters and stray newlines are normalised, and
    invisible padding is trimmed from the ends.
    """
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)
    text = re.sub(r"[ \t]+", " ", text).strip()
    while text and _invisible(text[0]):
        text = text[1:]
    while text and _invisible(text[-1]):
        text = text[:-1]
    return text


def _invisible(char: str) -> bool:
    """Whitespace or a Unicode format character (BOM, ZWSP, ZWJ, soft hyphen…)."""
    return char.isspace() or unicodedata.category(char) == "Cf"


def has_visible_text(text: Any) -> bool:
    """Whether anything a human would read survives.

    Image-only Douyin comments come back with a body made of invisible
    characters, which Python treats as content but JavaScript's `trim()` throws
    away. Deciding it here keeps the collected count and the analysed count
    from disagreeing.
    """
    return any(not _invisible(char) for char in ("" if text is None else str(text)))


def format_time(timestamp: Any) -> str:
    if not _as_int(timestamp):
        return ""
    try:
        return datetime.fromtimestamp(_as_int(timestamp)).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def parse_comment(item: dict[str, Any], *, aweme_id: str = "", source: str = "top",
                  parent_cid: str = "") -> Comment | None:
    """Turn one `comments[]` element into a Comment. Returns None if unusable."""
    if not isinstance(item, dict):
        return None
    cid = str(item.get("cid") or "").strip()
    if not cid:
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    return Comment(
        cid=cid,
        text=clean_text(item.get("text")),
        aweme_id=str(item.get("aweme_id") or aweme_id or ""),
        nickname=clean_text(user.get("nickname")),
        uid=str(user.get("uid") or ""),
        sec_uid=str(user.get("sec_uid") or ""),
        unique_id=str(user.get("unique_id") or ""),
        digg_count=_as_int(item.get("digg_count")),
        create_time=_as_int(item.get("create_time")),
        ip_label=clean_text(item.get("ip_label")),
        reply_total=_as_int(item.get("reply_comment_total")),
        level=_as_int(item.get("level"), 1) or 1,
        parent_cid=parent_cid or str(item.get("reply_id") or ""),
        source=source,
    )


def parse_comment_payload(payload: Any, *, aweme_id: str = "", source: str = "top",
                          parent_cid: str = "") -> list[Comment]:
    """Extract comments from a comment/list or comment/list/reply response body."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("comments")
    if not isinstance(raw, list):
        return []
    out: list[Comment] = []
    for item in raw:
        parsed = parse_comment(item, aweme_id=aweme_id, source=source, parent_cid=parent_cid)
        if parsed is not None:
            out.append(parsed)
    return out


def embedded_replies(item: dict[str, Any], *, aweme_id: str = "") -> list[Comment]:
    """Some responses inline a couple of replies under `reply_comment`."""
    parent = item.get("reply_comment")
    if not isinstance(parent, list):
        return []
    parent_cid = str(item.get("cid") or "")
    out: list[Comment] = []
    for reply in parent:
        parsed = parse_comment(reply, aweme_id=aweme_id, source="reply", parent_cid=parent_cid)
        if parsed is not None:
            out.append(parsed)
    return out


def dedupe_comments(comments: Iterable[Comment]) -> list[Comment]:
    """Drop repeats by cid, keeping first-seen order (which is Douyin's order)."""
    seen: set[str] = set()
    out: list[Comment] = []
    for comment in comments:
        if comment.cid in seen:
            continue
        seen.add(comment.cid)
        out.append(comment)
    return out


def split_analyzable(comments: Iterable[Comment]) -> tuple[list[Comment], int]:
    """Separate comments a text model can judge from ones it cannot.

    Douyin permits image-only comments, which arrive with a body that renders as
    nothing. The analyser rejects blank rows outright (a blank row in a
    hand-written CSV is almost always a mistake), so they are dropped here and
    reported instead.
    """
    keep: list[Comment] = []
    dropped = 0
    for comment in comments:
        if has_visible_text(comment.text):
            keep.append(comment)
        else:
            dropped += 1
    return keep, dropped


def looks_logged_in(cookies: Iterable[dict[str, Any]]) -> bool:
    names = {str(c.get("name") or "") for c in cookies if isinstance(c, dict)}
    return any(name in names for name in SESSION_COOKIES)


def is_blocked_page(title: str, body: str = "") -> bool:
    text = f"{title or ''} {(body or '')[:400]}"
    return any(marker in text for marker in _BLOCK_MARKERS)


def has_login_wall(body: str) -> bool:
    return bool(_LOGIN_HINT.search(body or ""))


def parse_works_payload(payload: Any) -> list[Work]:
    """Extract works from an `aweme/post` response body."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("aweme_list")
    if not isinstance(raw, list):
        return []
    out: list[Work] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        aweme_id = str(item.get("aweme_id") or "").strip()
        if not aweme_id:
            continue
        stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
        out.append(
            Work(
                aweme_id=aweme_id,
                desc=clean_text(item.get("desc")),
                create_time=_as_int(item.get("create_time")),
                comment_count=_as_int(stats.get("comment_count")),
                digg_count=_as_int(stats.get("digg_count")),
                share_url=str(item.get("share_url") or ""),
            )
        )
    return out


def extract_aweme_ids(html: str) -> list[str]:
    """Pull video ids out of a rendered page (used by the /hot fallback)."""
    found = re.findall(r"/video/(\d{15,25})", html or "")
    found += re.findall(r'"aweme_?[Ii]d":\s*"(\d{15,25})"', html or "")
    return list(dict.fromkeys(found))


def comment_rows(comments: Iterable[Comment], works: dict[str, Work] | None = None) -> list[list[str]]:
    """Build the CSV matrix handed to the analyser.

    Text is written raw: the analyser must see the comment exactly as the user
    wrote it. Formula-injection guarding happens later, on the final export the
    user actually opens in Excel.
    """
    works = works or {}
    rows: list[list[str]] = []
    for comment in comments:
        work = works.get(comment.aweme_id)
        rows.append(
            [
                comment.uid,
                comment.nickname,
                comment.profile_url,
                comment.text,
                str(comment.digg_count),
                format_time(comment.create_time),
                comment.ip_label,
                work.title if work else "",
                work.url if work else "",
                comment.cid,
            ]
        )
    return rows


def to_csv_text(rows: Iterable[Iterable[str]], header: Iterable[str] | None = None) -> str:
    """RFC4180 CSV with a negative lookahead-free, dependency-free writer."""
    import csv as _csv
    import io

    buffer = io.StringIO()
    writer = _csv.writer(buffer, lineterminator="\n")
    if header:
        writer.writerow(list(header))
    for row in rows:
        writer.writerow(list(row))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Browser side
# ---------------------------------------------------------------------------


class CollectorError(RuntimeError):
    """Raised for anything the user needs to act on (setup, login, risk control)."""


class PlaywrightMissing(CollectorError):
    pass


def _load_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise PlaywrightMissing(
            "抓取功能需要 Playwright。请先运行：bash tools/setup-collector.sh"
        ) from exc
    return sync_playwright


STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--autoplay-policy=no-user-gesture-required",
    "--mute-audio",
]

# Only these mean "the browser binary is not installed", which is the single
# case where silently falling back to the bundled Chromium is the right move.
MISSING_BROWSER_MARKERS = (
    "executable doesn't exist",
    "executable does not exist",
    "failed to launch chromium because executable",
    "no such file or directory",
    "cannot find the browser",
)

# Chrome refuses to share one profile directory between two processes and
# silently hands the URL to the running instance instead, so the debugging pipe
# never connects. That deserves a specific message, not a generic launch error.
PROFILE_BUSY_MARKERS = (
    "正在现有的浏览器会话中打开",
    "existing browser session",
    "profile appears to be in use",
    "process singleton",
)


class DouyinCollector:
    """Owns one persistent, logged-in Chrome. Must be used from a single thread."""

    def __init__(
        self,
        profile_dir: Path | str = DEFAULT_PROFILE_DIR,
        channel: str = DEFAULT_CHANNEL,
        headless: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.channel = channel
        self.headless = headless
        self._sleep = sleeper
        self._rng = rng or random.Random()
        self._pw = None
        self._context = None
        self._page = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._context is not None

    def start(self) -> None:
        if self.running:
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        sync_playwright = _load_playwright()
        pw = sync_playwright().start()
        try:
            context = self._launch(pw)
        except Exception:
            try:
                pw.stop()
            except Exception:
                pass
            raise
        self._pw = pw
        self._context = context
        self._context.add_init_script(STEALTH_SCRIPT)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def _launch(self, pw):
        """Launch with the real Chrome, falling back only when it is absent."""
        launch: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "args": LAUNCH_ARGS,
            "viewport": {"width": 1512, "height": 950},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        }
        if self.channel:
            launch["channel"] = self.channel
        try:
            return pw.chromium.launch_persistent_context(**launch)
        except Exception as exc:  # noqa: BLE001 - translated into actionable text
            message = str(exc)
            lowered = message.lower()
            if any(marker in message or marker in lowered for marker in PROFILE_BUSY_MARKERS):
                raise CollectorError(
                    f"浏览器配置目录 {self.profile_dir} 正被另一个进程占用。\n"
                    "抓取用的浏览器同一时间只能被一个进程使用：请先关掉已打开的抓取浏览器窗口，"
                    "或停止另一个正在运行的服务（网页界面上的「关闭浏览器」按钮也能释放它），再重试。"
                ) from exc
            if self.channel and any(marker in lowered for marker in MISSING_BROWSER_MARKERS):
                launch.pop("channel", None)
                return pw.chromium.launch_persistent_context(**launch)
            raise CollectorError(
                f"浏览器启动失败（channel={self.channel or 'bundled'}）：{message.strip().splitlines()[0]}"
            ) from exc

    def stop(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._context = None
        self._pw = None
        self._page = None

    # -- pacing ------------------------------------------------------------

    def pause(self, low: float, high: float | None = None) -> None:
        high = low if high is None else high
        self._sleep(round(self._rng.uniform(low, high), 3))

    # -- login -------------------------------------------------------------

    def session_cookies(self) -> list[dict[str, Any]]:
        if not self.running:
            return []
        try:
            return self._context.cookies(HOME_URL)
        except Exception:
            return []

    def logged_in(self) -> bool:
        return looks_logged_in(self.session_cookies())

    def open_login(self) -> str:
        """Open the site and, if needed, the QR login panel. Non-blocking."""
        self.start()
        page = self._page
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
        self.pause(2.5, 3.5)
        if not self.logged_in():
            for label in ("登录", "立即登录"):
                try:
                    target = page.get_by_text(label, exact=True).first
                    if target.count():
                        target.click(timeout=2500)
                        self.pause(1.0, 1.6)
                        break
                except Exception:
                    continue
        title = self._page_title()
        if is_blocked_page(title):
            raise CollectorError(
                "抖音返回了验证码页。请在打开的浏览器窗口里手动过一下验证，再重试。"
            )
        return title

    def wait_for_login(self, timeout: float = 240.0, poll: float = 2.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.logged_in():
                self.pause(1.0, 1.5)
                return True
            self._sleep(poll)
        return False

    def whoami(self) -> str:
        """Nickname of the logged-in account, or '' when not logged in."""
        if not self.running or not self.logged_in():
            return ""
        try:
            page = self._page
            page.goto(SELF_POSTS_URL, wait_until="domcontentloaded", timeout=45000)
            self.pause(3.0, 4.0)
            name = page.evaluate(
                "() => { const el = document.querySelector('[data-e2e=\"user-title\"], .user-title');"
                " if (el) return el.innerText.trim();"
                " const m = document.body.innerText.match(/^(.{1,20})\\n/); return m ? m[1] : ''; }"
            )
            return clean_text(name)
        except Exception:
            return ""

    # -- works -------------------------------------------------------------

    def list_works(self, limit: int = 60, max_scrolls: int = 8) -> list[Work]:
        """List the logged-in account's own works by watching `aweme/post`."""
        self.start()
        collected: list[Work] = []
        seen: set[str] = set()

        def on_response(response) -> None:
            if POSTS_API not in response.url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            for work in parse_works_payload(payload):
                if work.aweme_id in seen:
                    continue
                seen.add(work.aweme_id)
                collected.append(work)

        page = self._page
        page.on("response", on_response)
        try:
            page.goto(SELF_POSTS_URL, wait_until="domcontentloaded", timeout=45000)
            self.pause(4.0, 5.0)
            title = self._page_title()
            if is_blocked_page(title):
                raise CollectorError("抖音返回了验证码页，请在浏览器窗口里过验证后重试。")
            if not self.logged_in():
                raise CollectorError("还没有登录抖音。请先点「登录抖音」并用手机扫码。")
            for _ in range(max_scrolls):
                if len(collected) >= limit:
                    break
                page.mouse.wheel(0, 1200)
                self.pause(1.6, 2.6)
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
        return collected[:limit]

    # -- comments ----------------------------------------------------------

    def collect_comments(
        self,
        aweme_id: str,
        *,
        max_comments: int = 500,
        max_scrolls: int = 60,
        include_replies: bool = False,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> list[Comment]:
        """Scroll one video's comment panel and harvest what the page loads."""
        self.start()
        top_level: list[Comment] = []
        replies: list[Comment] = []
        seen: set[str] = set()
        videos_total = 0

        def absorb(comments: Iterable[Comment], bucket: list[Comment]) -> int:
            added = 0
            for comment in comments:
                if comment.cid in seen:
                    continue
                seen.add(comment.cid)
                bucket.append(comment)
                added += 1
            return added

        def on_response(response) -> None:
            nonlocal videos_total
            url = response.url
            if "douyin.com" not in url or "/comment/" not in url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            if REPLY_API in url:
                absorb(parse_comment_payload(payload, aweme_id=aweme_id, source="reply"), replies)
                return
            if COMMENT_API not in url:
                return
            total = _as_int(payload.get("total"))
            if total:
                videos_total = max(videos_total, total)
            absorb(parse_comment_payload(payload, aweme_id=aweme_id), top_level)
            for raw in payload.get("comments") or []:
                if isinstance(raw, dict):
                    absorb(embedded_replies(raw, aweme_id=aweme_id), replies)

        page = self._page
        page.on("response", on_response)
        try:
            page.goto(VIDEO_URL.format(aweme_id=aweme_id), wait_until="domcontentloaded", timeout=45000)
            self.pause(5.0, 6.5)
            title = self._page_title()
            if is_blocked_page(title):
                raise CollectorError("抖音返回了验证码页，请在浏览器窗口里过验证后重试。")

            stall = 0
            for _ in range(max_scrolls):
                before = len(top_level)
                page.mouse.wheel(0, 1100)
                self.pause(1.3, 2.4)
                if include_replies:
                    self._expand_replies()
                if on_progress is not None:
                    on_progress(len(top_level) + len(replies), title)
                if len(top_level) >= max_comments:
                    break
                stall = stall + 1 if len(top_level) == before else 0
                if stall >= 4 and len(top_level) > 0:
                    break
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

        top = dedupe_comments(top_level)[:max_comments]
        if not include_replies:
            return top
        return top + dedupe_comments(replies)

    def _expand_replies(self) -> int:
        """Best-effort: click 「展开N条回复」 so the page fetches reply pages.

        EXPERIMENTAL. Verified to locate the control, not verified to produce a
        reply response on every layout Douyin ships.
        """
        clicked = 0
        try:
            locator = self._page.get_by_text(re.compile(r"^展开.*回复"))
            for index in range(min(locator.count(), 3)):
                try:
                    locator.nth(index).click(timeout=1500)
                    clicked += 1
                    self.pause(0.8, 1.5)
                except Exception:
                    continue
        except Exception:
            pass
        return clicked

    # -- diagnostics -------------------------------------------------------

    def hot_video_ids(self, limit: int = 5) -> list[str]:
        """Anonymous entry point used by `doctor` when no account is available."""
        self.start()
        page = self._page
        page.goto(HOT_URL, wait_until="domcontentloaded", timeout=45000)
        self.pause(6.0, 7.5)
        return extract_aweme_ids(page.content())[:limit]

    def _page_title(self) -> str:
        try:
            return self._page.title() or ""
        except Exception:
            return ""


def probe_environment() -> dict[str, Any]:
    """Report what the collector can and cannot currently do."""
    info: dict[str, Any] = {"playwright": True, "channel": DEFAULT_CHANNEL}
    try:
        _load_playwright()
    except CollectorError as exc:
        return {"playwright": False, "error": str(exc), "channel": DEFAULT_CHANNEL}
    info["profile"] = str(DEFAULT_PROFILE_DIR)
    info["profile_exists"] = DEFAULT_PROFILE_DIR.exists()
    return info


def parse_works_response_text(body: str) -> list[Work]:
    try:
        return parse_works_payload(json.loads(body))
    except (json.JSONDecodeError, TypeError):
        return []
