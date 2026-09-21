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

# --- collection pacing --------------------------------------------------------
# The old loop slept a fixed 1.3-2.4s after every wheel, so a 60-scroll run cost
# ~110 seconds even when the network answered in 200ms. Instead we now wait for
# the page's *own* comment response and move on the instant it lands, keeping a
# small randomised floor so the pace still looks human. The wait values are
# upper bounds, not sleeps: a fast network finishes far sooner.
FIRST_COMMENT_WAIT_MS = 7000  # budget for the first comment page after navigation
SCROLL_WAIT_MS = 1800  # budget for the next page after each wheel tick
SCROLL_STEP = 2400  # pixels per wheel tick (was 1100, ~7 comments' worth)
SCROLL_FLOOR = (0.10, 0.25)  # minimum human-ish gap between ticks
POLL_STEP_MS = 120  # event-loop pump granularity while waiting
STALL_LIMIT = 4  # consecutive empty waits before we call the panel exhausted
# Douyin returns ~20 comments per page; this turns a target count into a scroll
# budget instead of the old flat 60 regardless of how much was asked for.
COMMENTS_PER_PAGE = 20
SCROLLS_OVERHEAD = 8

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


def parse_author_nickname(payload: Any) -> str:
    """Nickname of the account behind an `aweme/post` body.

    Reading it from the response is authoritative, unlike scraping the profile
    DOM, where the first line of body text can be a chrome string such as
    「开启读屏标签」 rather than the user's name.
    """
    if not isinstance(payload, dict):
        return ""
    for item in payload.get("aweme_list") or []:
        if not isinstance(item, dict):
            continue
        author = item.get("author")
        if isinstance(author, dict):
            name = clean_text(author.get("nickname"))
            if name:
                return name
    return ""


def scroll_budget(max_comments: int, *, per_page: int = COMMENTS_PER_PAGE) -> int:
    """Wheel ticks worth spending to reach `max_comments`.

    Crooked on purpose: pages arrive in bursts and the final page rarely lands
    exactly on the target, so we allow a margin. Clamped so a small target does
    not spin and a huge one does not run forever.
    """
    target = max(1, int(max_comments))
    pages = -(-target // max(1, per_page))  # ceil
    return max(12, min(400, pages + SCROLLS_OVERHEAD))


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
        """Nickname of the logged-in account, or '' when not logged in.

        Read from the account's own `aweme/post` payload, which is authoritative.
        The profile DOM is a poor source: its first line of body text can be a
        chrome string like 「开启读屏标签」, which is what the previous
        first-line fallback reported as the user's name.
        """
        if not self.running or not self.logged_in():
            return ""
        found = ""

        def on_response(response) -> None:
            nonlocal found
            if POSTS_API not in response.url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            name = parse_author_nickname(payload)
            if name and not found:
                found = name

        page = self._page
        page.on("response", on_response)
        try:
            page.goto(SELF_POSTS_URL, wait_until="domcontentloaded", timeout=45000)
            waited = 0
            while waited < FIRST_COMMENT_WAIT_MS and not found:
                page.wait_for_timeout(POLL_STEP_MS)
                waited += POLL_STEP_MS
            if not found:
                # Account has no works yet, so the payload never arrives. Fall
                # back to an explicit nickname node only, never body text.
                node_js = (
                    "() => {"
                    " const sel = ['[data-e2e=\"user-info\"] [data-e2e=\"user-title\"]',"
                    " '[data-e2e=\"user-title\"]', '.user-info .user-name', '.user-name'].join(',');"
                    " const el = document.querySelector(sel);"
                    " return el ? el.innerText.trim() : ''; }"
                )
                found = clean_text(page.evaluate(node_js))
            return found
        except Exception:
            return ""
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    # -- works -------------------------------------------------------------

    def list_works(self, limit: int = 60, max_scrolls: int = 8) -> list[Work]:
        """List the logged-in account's own works by watching `aweme/post`."""
        self.start()
        collected: list[Work] = []
        seen: set[str] = set()
        response_count = 0

        def on_response(response) -> None:
            nonlocal response_count
            if POSTS_API not in response.url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            response_count += 1
            for work in parse_works_payload(payload):
                if work.aweme_id in seen:
                    continue
                seen.add(work.aweme_id)
                collected.append(work)

        page = self._page
        page.on("response", on_response)
        try:
            page.goto(SELF_POSTS_URL, wait_until="domcontentloaded", timeout=45000)
            self._await_response(page, lambda: response_count, mark=0, timeout_ms=FIRST_COMMENT_WAIT_MS)
            title = self._page_title()
            if is_blocked_page(title):
                raise CollectorError("抖音返回了验证码页，请在浏览器窗口里过验证后重试。")
            if not self.logged_in():
                raise CollectorError("还没有登录抖音。请先点「登录抖音」并用手机扫码。")
            stall = 0
            for _ in range(max_scrolls):
                if len(collected) >= limit:
                    break
                mark = response_count
                page.mouse.wheel(0, SCROLL_STEP)
                if self._await_response(page, lambda: response_count, mark=mark, timeout_ms=SCROLL_WAIT_MS):
                    stall = 0
                else:
                    stall += 1
                self.pause(*SCROLL_FLOOR)
                if stall >= STALL_LIMIT:
                    break
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
        max_scrolls: int | None = None,
        include_replies: bool = False,
        on_progress: Callable[[int, str], None] | None = None,
    ) -> list[Comment]:
        """Scroll one video's comment panel and harvest what the page loads."""
        self.start()
        top_level: list[Comment] = []
        replies: list[Comment] = []
        seen: set[str] = set()
        videos_total = 0
        response_count = 0  # bumped per comment payload; drives the adaptive waits

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
            nonlocal videos_total, response_count
            url = response.url
            if "douyin.com" not in url or "/comment/" not in url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            if REPLY_API in url:
                absorb(parse_comment_payload(payload, aweme_id=aweme_id, source="reply"), replies)
                response_count += 1
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
            response_count += 1

        page = self._page
        page.on("response", on_response)
        try:
            page.goto(VIDEO_URL.format(aweme_id=aweme_id), wait_until="domcontentloaded", timeout=45000)
            # Wait for the first comment page instead of a blind 5-6.5s sleep:
            # on a warm connection this returns in well under a second. The
            # container check covers videos whose comments are empty, where no
            # comment request is ever made.
            self._await_response(
                page,
                lambda: response_count,
                mark=0,
                timeout_ms=FIRST_COMMENT_WAIT_MS,
                also_ready=self._comment_list_present,
            )
            title = self._page_title()
            if is_blocked_page(title):
                raise CollectorError("抖音返回了验证码页，请在浏览器窗口里过验证后重试。")

            # Aim the wheel at the comment scroller. Wheel events go to whatever
            # is under the cursor, so without this the page body can absorb them
            # and no new comments load at all (looks exactly like "very slow").
            self._hover_comment_panel()

            budget = max_scrolls if max_scrolls is not None else scroll_budget(max_comments)
            stall = 0
            for _ in range(budget):
                mark = response_count
                before = len(top_level) + len(replies)
                page.mouse.wheel(0, SCROLL_STEP)
                # Move on the moment the page's own payload lands, instead of
                # sleeping a fixed amount and hoping it was enough.
                self._await_response(page, lambda: response_count, mark=mark, timeout_ms=SCROLL_WAIT_MS)
                # Progress is measured in *new comments*, not in responses: an
                # exhausted panel can keep answering with empty pages, and
                # counting those would never look like a stall.
                grew = len(top_level) + len(replies) > before
                if grew:
                    stall = 0
                else:
                    stall += 1
                    # Pinned to the end with nothing new means there is nothing
                    # left to load: stop now instead of waiting out the budget.
                    if self._at_scroll_bottom():
                        break
                self.pause(*SCROLL_FLOOR)  # keep a human-ish floor between ticks
                if include_replies:
                    self._expand_replies()
                if on_progress is not None:
                    on_progress(len(top_level) + len(replies), title)
                # Stop as soon as the target is met, or once the reported total
                # has all arrived, or after repeated empty waits. The empty-wait
                # break is unconditional: the initial wait above already gave the
                # first page its chance, so a stalled panel means we are done.
                if len(top_level) >= max_comments:
                    break
                if videos_total and len(top_level) >= videos_total:
                    break
                if stall >= STALL_LIMIT:
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

    def _await_response(
        self,
        page,
        counter: Callable[[], int],
        *,
        mark: int,
        timeout_ms: int,
        also_ready: Callable[[], bool] | None = None,
        poll_ready_every: int = 4,
    ) -> bool:
        """Pump the event loop until a response lands past `mark`, else time out.

        `page.wait_for_timeout` is what dispatches Playwright's events, so a
        plain `time.sleep` here would stall delivery of the very responses we
        are waiting for. Returns True if new data (or the alternative signal)
        arrived.

        `also_ready` is an escape hatch for the first page: a video with zero
        comments never fires a comment request at all, so waiting purely on the
        counter would burn the whole budget every time. It is polled every
        `poll_ready_every` pumps because each check costs a round-trip.
        """
        waited = 0
        guard = 0
        pumps = 0
        while waited < timeout_ms:
            if counter() > mark:
                return True
            pumps += 1
            if also_ready is not None and pumps % poll_ready_every == 0 and also_ready():
                return True
            step = min(POLL_STEP_MS, timeout_ms - waited)
            try:
                page.wait_for_timeout(step)
            except Exception:
                return counter() > mark
            waited += step
            guard += 1
            if guard > 400:  # belt and braces against a pathological clock
                break
        return counter() > mark

    COMMENT_LIST_JS = """
    () => !!document.querySelector('[data-e2e="comment-list"], [data-e2e="comment-item"]')
    """

    def _comment_list_present(self) -> bool:
        """True once the comment container is on the page, empty or not."""
        try:
            return bool(self._page.evaluate(self.COMMENT_LIST_JS))
        except Exception:
            return False

    COMMENT_LIST_SELECTOR = '[data-e2e="comment-list"], [data-e2e="comment-item"]'

    COMMENT_PANEL_JS = """
    () => {
      const hittable = (el) => {
        const style = getComputedStyle(el);
        if (!/(auto|scroll)/.test(style.overflowY)) return 0;
        return el.scrollHeight - el.clientHeight;
      };
      // Preferred: walk up from the comment list to whoever actually scrolls it.
      // On the current layout that is the page-level route container, not an
      // inner panel, so a "panel on the right half" guess finds nothing.
      let node = document.querySelector('[data-e2e="comment-list"], [data-e2e="comment-item"]');
      while (node && node !== document.body) {
        if (hittable(node) > 40) {
          const r = node.getBoundingClientRect();
          return {
            x: r.left + r.width / 2,
            y: r.top + Math.min(r.height / 2, 260),
            how: 'comment-ancestor',
          };
        }
        node = node.parentElement;
      }
      // Fallback: the deepest scrollable box that is not the left nav rail.
      let best = null;
      for (const el of document.querySelectorAll('div, main, section')) {
        if (hittable(el) < 300) continue;
        if (el.closest('[data-e2e="douyin-navigation"]')) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 300 || r.height < 300) continue;
        if (!best || el.scrollHeight > best.scrollHeight) best = el;
      }
      if (!best) return null;
      const r = best.getBoundingClientRect();
      return {
        x: r.left + r.width / 2,
        y: r.top + Math.min(r.height / 2, 260),
        how: 'largest-scroller',
      };
    }
    """

    def _hover_comment_panel(self) -> bool:
        """Park the cursor over whatever actually scrolls the comment list.

        Wheel events only reach the scroller under the cursor, so without this
        the page body can absorb every tick and nothing new loads — which looks
        exactly like "the scraper is very slow".
        """
        try:
            box = self._page.evaluate(self.COMMENT_PANEL_JS)
        except Exception:
            return False
        if not isinstance(box, dict):
            return False
        try:
            self._page.mouse.move(box["x"], box["y"])
            return True
        except Exception:
            return False

    AT_BOTTOM_JS = """
    () => {
      const anchor = document.querySelector('[data-e2e="comment-list"], [data-e2e="comment-item"]');
      // Not rendered yet: do not claim we are done, or we would quit early.
      if (!anchor) return false;
      const hittable = (el) => {
        const style = getComputedStyle(el);
        if (!/(auto|scroll)/.test(style.overflowY)) return 0;
        return el.scrollHeight - el.clientHeight;
      };
      let node = anchor;
      while (node && node !== document.body) {
        if (hittable(node) > 40) break;
        node = node.parentElement;
      }
      // No scrollable ancestor at all: the comments already fit on screen, so
      // there is nothing left to load. This is the common case for a video with
      // a handful of comments, and reporting it avoids waiting out the stall
      // budget on every such video.
      if (!node || node === document.body) return true;
      // A few pixels of slack: sub-pixel layout and lazy-load spinners mean the
      // scroller rarely reports an exact 0 remaining.
      return node.scrollTop + node.clientHeight >= node.scrollHeight - 8;
    }
    """

    def _at_scroll_bottom(self) -> bool:
        """True when the comment scroller is pinned to its end.

        Used to end the run on the first empty tick instead of waiting out the
        stall budget, which is pure dead time on a fully-read video.
        """
        try:
            return bool(self._page.evaluate(self.AT_BOTTOM_JS))
        except Exception:
            return False

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
