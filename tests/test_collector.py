"""Contract tests for the Douyin collector.

Every JSON sample in here is copied from a real `aweme/v1/web/comment/list/`
and `aweme/v1/web/aweme/post/` response captured while probing the live site,
with only the irrelevant keys trimmed. That keeps the parser pinned to reality
without needing a browser, an account, or network access during tests.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector import douyin  # noqa: E402


# --- real shapes -----------------------------------------------------------

COMMENT_ITEM = {
    "cid": "7686753853958161189",
    "text": "其实仔细想想也不觉得解气，自己被迫变成了小时候最恐惧的人。",
    "aweme_id": "7686658435312030373",
    "create_time": 1789711848,
    "digg_count": 1118,
    "status": 1,
    "reply_id": "0",
    "reply_comment_total": 12,
    "item_comment_total": 467,
    "ip_label": "广东",
    "level": 1,
    "is_hot": True,
    "reply_comment": None,
    "user": {
        "uid": "2104916854449450",
        "short_id": "37548158541",
        "nickname": "柒柒上班辛苦了",
        "unique_id": "37548158541",
        "region": "CN",
        "sec_uid": "MS4wLjABAAAA4y3os9ZGJTrE-kIrwi5yS7Cw8KWdrTAyk75-yJohwb-wp_zai6wBsZ47yevN5cPm",
    },
}

REPLY_ITEM = {
    "cid": "7686760000000000001",
    "text": "同感，我也这么觉得",
    "aweme_id": "7686658435312030373",
    "create_time": 1789712000,
    "digg_count": 3,
    "reply_id": "7686753853958161189",
    "reply_comment_total": 0,
    "level": 2,
    "user": {"uid": "1", "nickname": "路人甲", "sec_uid": "SEC_REPLY", "unique_id": ""},
}

COMMENT_WITH_EMBEDDED_REPLY = dict(
    COMMENT_ITEM,
    cid="7686753853958161199",
    text="土豆丝和萝卜丝不需要煮一下吗",
    digg_count=0,
    ip_label="上海",
    reply_comment=[REPLY_ITEM],
)

WORKS_ITEM = {
    "aweme_id": "7686658435312030373",
    "desc": "早餐不知道吃什么就来做土豆丝饼吧好吃的很 #土豆丝饼 #早餐饼\n第二行不该出现在标题里",
    "create_time": 1789700000,
    "statistics": {"comment_count": 467, "digg_count": 12234, "share_count": 33},
    "share_url": "https://www.iesdouyin.com/share/video/7686658435312030373/",
}


class ParseCommentTests(unittest.TestCase):
    def test_real_comment_maps_every_field(self):
        comment = douyin.parse_comment(COMMENT_ITEM)
        assert comment is not None
        self.assertEqual(comment.cid, "7686753853958161189")
        self.assertEqual(comment.nickname, "柒柒上班辛苦了")
        self.assertEqual(comment.uid, "2104916854449450")
        self.assertEqual(comment.sec_uid, "MS4wLjABAAAA4y3os9ZGJTrE-kIrwi5yS7Cw8KWdrTAyk75-yJohwb-wp_zai6wBsZ47yevN5cPm")
        self.assertEqual(comment.digg_count, 1118)
        self.assertEqual(comment.reply_total, 12)
        self.assertEqual(comment.ip_label, "广东")
        self.assertEqual(comment.level, 1)
        self.assertEqual(comment.source, "top")

    def test_profile_url_is_built_from_sec_uid(self):
        comment = douyin.parse_comment(COMMENT_ITEM)
        assert comment is not None
        self.assertTrue(comment.profile_url.endswith(
            "/user/MS4wLjABAAAA4y3os9ZGJTrE-kIrwi5yS7Cw8KWdrTAyk75-yJohwb-wp_zai6wBsZ47yevN5cPm"
        ))

    def test_identity_prefers_sec_uid_then_uid_then_nickname(self):
        full = douyin.parse_comment(COMMENT_ITEM)
        no_sec = douyin.parse_comment({"cid": "9", "user": {"uid": "77", "nickname": "n"}})
        only_nick = douyin.parse_comment({"cid": "9", "user": {"nickname": "n"}})
        assert full and no_sec and only_nick
        self.assertEqual(full.identity, full.sec_uid)
        self.assertEqual(no_sec.identity, "77")
        self.assertEqual(only_nick.identity, "n")

    def test_comments_without_cid_are_dropped(self):
        self.assertIsNone(douyin.parse_comment({"text": "没有 cid"}))
        self.assertIsNone(douyin.parse_comment({"cid": "   ", "text": "空 cid"}))
        self.assertIsNone(douyin.parse_comment("not a dict"))  # type: ignore[arg-type]

    def test_missing_user_object_does_not_explode(self):
        comment = douyin.parse_comment({"cid": "5", "text": "匿名"})
        assert comment is not None
        self.assertEqual(comment.nickname, "")
        self.assertEqual(comment.sec_uid, "")
        self.assertEqual(comment.profile_url, "")

    def test_defensive_ints(self):
        comment = douyin.parse_comment(
            {"cid": "5", "digg_count": "abc", "reply_comment_total": None, "level": "2"}
        )
        assert comment is not None
        self.assertEqual(comment.digg_count, 0)
        self.assertEqual(comment.reply_total, 0)
        self.assertEqual(comment.level, 2)

    def test_level_defaults_to_one_when_absent(self):
        comment = douyin.parse_comment({"cid": "5"})
        assert comment is not None
        self.assertEqual(comment.level, 1)


class CleanTextTests(unittest.TestCase):
    def test_emoji_are_signal_and_survive(self):
        raw = "[炸弹][流泪][比心] 这个饼真的太香了吧"
        self.assertEqual(douyin.clean_text(raw), raw)

    def test_whitespace_is_collapsed_but_newlines_kept(self):
        self.assertEqual(douyin.clean_text("a   \t b"), "a b")
        self.assertEqual(douyin.clean_text(" a\r\nb "), "a\nb")

    def test_control_characters_are_stripped(self):
        self.assertEqual(douyin.clean_text("好\x00的\x07"), "好的")

    def test_none_becomes_empty_string(self):
        self.assertEqual(douyin.clean_text(None), "")


class FormatTimeTests(unittest.TestCase):
    def test_unix_timestamp_becomes_readable_local_time(self):
        self.assertRegex(douyin.format_time(1789711848), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_zero_and_garbage_become_empty(self):
        self.assertEqual(douyin.format_time(0), "")
        self.assertEqual(douyin.format_time(None), "")
        self.assertEqual(douyin.format_time("x"), "")


class ParsePayloadTests(unittest.TestCase):
    def test_comment_list_payload(self):
        payload = {"comments": [COMMENT_ITEM], "total": 467, "cursor": 20, "has_more": 1}
        parsed = douyin.parse_comment_payload(payload, aweme_id="7686658435312030373")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].aweme_id, "7686658435312030373")

    def test_empty_and_malformed_payloads(self):
        for payload in ({}, {"comments": None}, {"comments": []}, [], None, "x"):
            with self.subTest(payload=payload):
                self.assertEqual(douyin.parse_comment_payload(payload), [])

    def test_aweme_id_falls_back_to_the_argument(self):
        payload = {"comments": [{"cid": "1", "text": "x"}]}
        parsed = douyin.parse_comment_payload(payload, aweme_id="999")
        self.assertEqual(parsed[0].aweme_id, "999")

    def test_embedded_replies_are_picked_up_from_the_parent(self):
        replies = douyin.embedded_replies(COMMENT_WITH_EMBEDDED_REPLY, aweme_id="7686658435312030373")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].source, "reply")
        self.assertEqual(replies[0].parent_cid, "7686753853958161199")

    def test_parent_without_reply_comment_yields_nothing(self):
        self.assertEqual(douyin.embedded_replies(COMMENT_ITEM), [])

    def test_reply_payload_marks_source(self):
        parsed = douyin.parse_comment_payload(
            {"comments": [REPLY_ITEM]}, aweme_id="1", source="reply", parent_cid="p1"
        )
        self.assertEqual(parsed[0].source, "reply")
        self.assertEqual(parsed[0].parent_cid, "p1")


class DedupeTests(unittest.TestCase):
    def test_order_is_first_seen_and_repeats_drop(self):
        items = [{"cid": "a"}, {"cid": "b"}, {"cid": "a"}, {"cid": "c"}, {"cid": "b"}]
        comments = [douyin.parse_comment(i) for i in items]
        result = douyin.dedupe_comments([c for c in comments if c])
        self.assertEqual([c.cid for c in result], ["a", "b", "c"])

    def test_empty_input(self):
        self.assertEqual(douyin.dedupe_comments([]), [])


class SplitAnalyzableTests(unittest.TestCase):
    """Image-only comments arrive with no readable text and must not reach the analyser."""

    def test_text_comments_are_kept_and_empty_ones_counted(self):
        comments = [
            douyin.parse_comment({"cid": "1", "text": "有文字"}),
            douyin.parse_comment({"cid": "2", "text": ""}),
            douyin.parse_comment({"cid": "3", "text": "   "}),
            douyin.parse_comment({"cid": "4", "text": "[炸弹]"}),
        ]
        keep, dropped = douyin.split_analyzable([c for c in comments if c])
        self.assertEqual([c.cid for c in keep], ["1", "4"])
        self.assertEqual(dropped, 2)

    def test_invisible_only_body_counts_as_no_text(self):
        """Real Douyin image comments are padded with invisible characters.

        Python sees a non-empty string here while JavaScript's trim() sees
        nothing, which used to make the collected and analysed counts disagree.
        """
        for body in ["\ufeff", "\u200b", "\u200b\u200d\ufeff", "\u00ad", "\u2060\u200c"]:
            with self.subTest(body=repr(body)):
                comment = douyin.parse_comment({"cid": "1", "text": body})
                keep, dropped = douyin.split_analyzable([comment])
                self.assertEqual(keep, [])
                self.assertEqual(dropped, 1)

    def test_clean_text_trims_invisible_padding_but_keeps_the_content(self):
        self.assertEqual(douyin.clean_text("\ufeff怎么买？\u200b"), "怎么买？")
        self.assertEqual(douyin.clean_text("\u200b你好\u2060"), "你好")

    def test_emoji_only_comment_is_still_analyzable(self):
        comment = douyin.parse_comment({"cid": "1", "text": "[爱心][爱心]"})
        keep, dropped = douyin.split_analyzable([comment])
        self.assertEqual(len(keep), 1)
        self.assertEqual(dropped, 0)

    def test_zwj_inside_a_real_emoji_sequence_is_preserved(self):
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        self.assertTrue(douyin.has_visible_text(family))
        keep, _ = douyin.split_analyzable([douyin.parse_comment({"cid": "1", "text": family})])
        self.assertEqual(keep[0].text, family)

    def test_nothing_to_keep(self):
        keep, dropped = douyin.split_analyzable([douyin.parse_comment({"cid": "1", "text": ""})])
        self.assertEqual(keep, [])
        self.assertEqual(dropped, 1)

    def test_every_emitted_row_has_text_the_analyser_will_accept(self):
        comments = [
            douyin.parse_comment({"cid": "1", "text": "怎么买"}),
            douyin.parse_comment({"cid": "2", "text": ""}),
            douyin.parse_comment({"cid": "3", "text": "\ufeff"}),
        ]
        keep, _ = douyin.split_analyzable([c for c in comments if c])
        rows = douyin.comment_rows(keep, {})
        self.assertTrue(all(row[3].strip() for row in rows))


class LoginDetectionTests(unittest.TestCase):
    def test_session_cookie_means_logged_in(self):
        cookies = [{"name": "sessionid"}, {"name": "ttwid"}]
        self.assertTrue(douyin.looks_logged_in(cookies))

    def test_anonymous_cookies_do_not(self):
        cookies = [{"name": "ttwid"}, {"name": "__ac_nonce"}, {"name": "passport_csrf_token"}]
        self.assertFalse(douyin.looks_logged_in(cookies))

    def test_garbage_input_is_safe(self):
        self.assertFalse(douyin.looks_logged_in([None, "x", {}]))  # type: ignore[list-item]

    def test_blocked_page_detection(self):
        self.assertTrue(douyin.is_blocked_page("验证码中间页"))
        self.assertTrue(douyin.is_blocked_page("抖音", "请完成安全验证"))
        self.assertFalse(douyin.is_blocked_page("抖音精选电脑版 - 抖音旗下优质视频平台"))

    def test_login_wall_detection(self):
        self.assertTrue(douyin.has_login_wall("扫码登录 立即登录"))
        self.assertFalse(douyin.has_login_wall("精选 推荐 关注"))


class ParseWorksTests(unittest.TestCase):
    def test_real_work_item(self):
        works = douyin.parse_works_payload({"aweme_list": [WORKS_ITEM], "has_more": 1})
        self.assertEqual(len(works), 1)
        work = works[0]
        self.assertEqual(work.aweme_id, "7686658435312030373")
        self.assertEqual(work.comment_count, 467)
        self.assertEqual(work.digg_count, 12234)
        self.assertEqual(work.url, "https://www.douyin.com/video/7686658435312030373")

    def test_title_uses_only_the_first_line_and_is_capped(self):
        work = douyin.parse_works_payload({"aweme_list": [WORKS_ITEM]})[0]
        self.assertNotIn("\n", work.title)
        self.assertIn("土豆丝饼", work.title)
        long_work = douyin.Work(aweme_id="1", desc="字" * 200)
        self.assertLessEqual(len(long_work.title), 60)

    def test_empty_desc_gets_a_placeholder_title(self):
        work = douyin.Work(aweme_id="123")
        self.assertEqual(work.title, "作品 123")

    def test_missing_statistics_does_not_explode(self):
        works = douyin.parse_works_payload({"aweme_list": [{"aweme_id": "7", "desc": "x"}]})
        self.assertEqual(works[0].comment_count, 0)

    def test_malformed_payloads(self):
        for payload in ({}, {"aweme_list": None}, [], None):
            with self.subTest(payload=payload):
                self.assertEqual(douyin.parse_works_payload(payload), [])

    def test_items_without_aweme_id_are_skipped(self):
        self.assertEqual(douyin.parse_works_payload({"aweme_list": [{"desc": "x"}]}), [])

    def test_parse_works_response_text(self):
        body = json.dumps({"aweme_list": [WORKS_ITEM]})
        self.assertEqual(len(douyin.parse_works_response_text(body)), 1)
        self.assertEqual(douyin.parse_works_response_text("not json"), [])


class ExtractAwemeIdTests(unittest.TestCase):
    def test_finds_ids_from_hrefs_and_state_blobs(self):
        html = (
            '<a href="/video/7685573415033942245">x</a>'
            '<script>{"awemeId":"7684981076503123323"}</script>'
        )
        self.assertEqual(
            douyin.extract_aweme_ids(html),
            ["7685573415033942245", "7684981076503123323"],
        )

    def test_dedupes_and_ignores_short_numbers(self):
        html = '<a href="/video/7685573415033942245">a</a><a href="/video/7685573415033942245">b</a><a href="/video/123">c</a>'
        self.assertEqual(douyin.extract_aweme_ids(html), ["7685573415033942245"])


class CommentRowsTests(unittest.TestCase):
    def setUp(self):
        self.comments = [
            douyin.parse_comment(COMMENT_ITEM),
            douyin.parse_comment(COMMENT_WITH_EMBEDDED_REPLY),
        ]
        self.works = {
            "7686658435312030373": douyin.parse_works_payload({"aweme_list": [WORKS_ITEM]})[0]
        }

    def test_header_is_the_single_source_of_truth(self):
        matrix = douyin.comment_rows([c for c in self.comments if c], self.works)
        self.assertEqual(len(matrix[0]), len(douyin.CSV_HEADER))
        for row in matrix:
            self.assertEqual(len(row), len(douyin.CSV_HEADER))

    def test_identity_columns_are_present_so_the_loop_closes(self):
        row = douyin.comment_rows([self.comments[0]], self.works)[0]
        self.assertEqual(row[0], "2104916854449450")   # uid
        self.assertEqual(row[1], "柒柒上班辛苦了")       # nickname
        self.assertIn("/user/MS4wLjABAAAA", row[2])     # profile link

    def test_comment_text_is_written_raw(self):
        """The analyser must see the comment exactly as the user wrote it."""
        tricky = douyin.parse_comment({"cid": "1", "text": "=1+1 这是公式开头"})
        row = douyin.comment_rows([tricky], {})[0]
        self.assertEqual(row[3], "=1+1 这是公式开头")
        self.assertFalse(row[3].startswith("'"))

    def test_work_columns_are_filled_from_the_map(self):
        row = douyin.comment_rows([self.comments[0]], self.works)[0]
        self.assertIn("土豆丝饼", row[7])
        self.assertEqual(row[8], "https://www.douyin.com/video/7686658435312030373")

    def test_missing_work_leaves_those_columns_blank(self):
        row = douyin.comment_rows([self.comments[0]], {})[0]
        self.assertEqual(row[7], "")
        self.assertEqual(row[8], "")

    def test_only_one_header_may_contain_the_word_评论(self):
        """The analyser auto-detects the comment column by that substring."""
        matches = [name for name in douyin.CSV_HEADER if "评论" in name]
        self.assertEqual(matches, ["评论内容"])


class CsvTests(unittest.TestCase):
    def test_round_trip_with_awkward_text(self):
        import csv
        import io

        rows = [["昵称", "他说：\"贵，但是,想买\"\n第二行"]]
        text = douyin.to_csv_text(rows, ["昵称", "评论内容"])
        parsed = list(csv.reader(io.StringIO(text)))
        self.assertEqual(parsed[0], ["昵称", "评论内容"])
        self.assertEqual(parsed[1][1], '他说："贵，但是,想买"\n第二行')

    def test_header_only(self):
        text = douyin.to_csv_text([], ["a", "b"])
        self.assertEqual(text, "a,b\n")


class LaunchFallbackTests(unittest.TestCase):
    """The channel fallback once fired on *every* failure, because the launch
    log always contains a path with the word "chrome" in it. That silently
    swapped the real Chrome for the bundled Chromium and hid the real cause."""

    class _StubChromium:
        def __init__(self, errors):
            self.errors = list(errors)
            self.calls: list[dict] = []

        def launch_persistent_context(self, **kwargs):
            self.calls.append(kwargs)
            error = self.errors.pop(0) if self.errors else None
            if error is not None:
                raise error
            return "context"

    class _StubPlaywright:
        def __init__(self, chromium):
            self.chromium = chromium

    def _collector(self, channel="chrome"):
        return douyin.DouyinCollector(profile_dir="/tmp/radar-test-profile", channel=channel)

    def test_missing_system_chrome_falls_back_to_the_bundled_build(self):
        chromium = self._StubChromium(
            [Exception("Executable doesn't exist at /Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), None]
        )
        result = self._collector()._launch(self._StubPlaywright(chromium))
        self.assertEqual(result, "context")
        self.assertEqual(len(chromium.calls), 2)
        self.assertEqual(chromium.calls[0].get("channel"), "chrome")
        self.assertNotIn("channel", chromium.calls[1])

    def test_busy_profile_gets_an_actionable_message_and_no_retry(self):
        chromium = self._StubChromium(
            [Exception("Browser logs:\n  - [pid=1][out] 正在现有的浏览器会话中打开。")]
        )
        with self.assertRaises(douyin.CollectorError) as ctx:
            self._collector()._launch(self._StubPlaywright(chromium))
        self.assertIn("正被另一个进程占用", str(ctx.exception))
        self.assertEqual(len(chromium.calls), 1, "不能把配置被占用悄悄换成另一个内核重试")

    def test_an_error_that_merely_mentions_chrome_is_not_swallowed(self):
        chromium = self._StubChromium(
            [
                Exception(
                    "Target page, context or browser has been closed\n"
                    "<launching> /Applications/Google Chrome.app/Contents/MacOS/Google Chrome for Testing"
                )
            ]
        )
        with self.assertRaises(douyin.CollectorError) as ctx:
            self._collector()._launch(self._StubPlaywright(chromium))
        self.assertIn("浏览器启动失败", str(ctx.exception))
        self.assertEqual(len(chromium.calls), 1)

    def test_no_channel_means_no_channel_key(self):
        chromium = self._StubChromium([None])
        self._collector(channel="")._launch(self._StubPlaywright(chromium))
        self.assertNotIn("channel", chromium.calls[0])

    def test_profile_directory_is_passed_through(self):
        chromium = self._StubChromium([None])
        self._collector()._launch(self._StubPlaywright(chromium))
        self.assertEqual(chromium.calls[0]["user_data_dir"], "/tmp/radar-test-profile")
        self.assertFalse(chromium.calls[0]["headless"], "抓取必须是有头浏览器，扫码登录需要界面")


class ServiceShapeTests(unittest.TestCase):
    def test_service_status_defaults_are_json_safe(self):
        from collector.service import CollectorService

        service = CollectorService(profile_dir="/tmp/radar-test-profile")
        payload = service._status.as_dict()
        json.dumps(payload)  # must not raise
        self.assertFalse(payload["running"])
        self.assertFalse(payload["loggedIn"])
        self.assertEqual(payload["profile"], "/tmp/radar-test-profile")

    def test_unknown_operation_is_rejected(self):
        from collector.service import CollectorService

        service = CollectorService(profile_dir="/tmp/radar-test-profile")
        with self.assertRaises(ValueError):
            service._dispatch("nonsense", {}, None)

    def test_work_dict_is_what_the_frontend_expects(self):
        from collector.service import CollectorService

        work = douyin.parse_works_payload({"aweme_list": [WORKS_ITEM]})[0]
        payload = CollectorService._work_dict(work)
        self.assertEqual(
            sorted(payload),
            ["awemeId", "commentCount", "createTime", "createdAt", "desc", "diggCount", "title", "url"],
        )
        self.assertEqual(payload["awemeId"], "7686658435312030373")
        json.dumps(payload, ensure_ascii=False)


class EnvironmentProbeTests(unittest.TestCase):
    def test_probe_reports_playwright_availability_without_raising(self):
        info = douyin.probe_environment()
        self.assertIn("playwright", info)
        self.assertIn("channel", info)
        if not info["playwright"]:
            self.assertIn("error", info)


class CollectProgressTitleTests(unittest.TestCase):
    """Progress must name the work we are actually on.

    Regression: the title came from the page, which lags on this SPA, so the
    first ticks of every work displayed the previous video's name.
    """

    class _FakeCollector:
        def __init__(self):
            # The real collector records titles it learns from `aweme/detail`
            # here; the service folds them in when titling link-sourced rows.
            self.video_meta = {}

        def collect_comments(self, _aweme_id, **kwargs):
            kwargs["on_progress"](7, "从页面读到的滞后标题")
            return []

    def _titles(self, works):
        from collector.service import CollectorService

        service = CollectorService(profile_dir="/tmp/radar-test-profile")
        events: list[dict] = []
        service._collect(
            self._FakeCollector(),
            {
                "awemeIds": ["111"],
                "works": works,
                "maxComments": 20,
            },
            events.append,
        )
        return [event["title"] for event in events if event.get("type") == "progress"]

    def test_prefers_the_title_we_already_know(self):
        self.assertEqual(self._titles([{"aweme_id": "111", "desc": "我自己的作品标题"}]), ["我自己的作品标题"])

    def test_falls_back_to_the_page_title_for_an_unknown_work(self):
        self.assertEqual(self._titles([]), ["从页面读到的滞后标题"])

    def test_falls_back_when_the_known_title_is_blank(self):
        self.assertEqual(self._titles([{"aweme_id": "111", "desc": ""}]), ["从页面读到的滞后标题"])


class ParseAuthorNicknameTests(unittest.TestCase):
    """The nickname must come from the payload, not the profile DOM.

    Regression: the profile page's first line of body text is chrome such as
    「开启读屏标签」, which the old fallback happily reported as the user's name.
    """

    def test_reads_nickname_from_first_work_author(self):
        payload = {"aweme_list": [{"author": {"nickname": "天工造神局"}}]}
        self.assertEqual(douyin.parse_author_nickname(payload), "天工造神局")

    def test_skips_entries_without_an_author_until_one_has_a_name(self):
        payload = {
            "aweme_list": [
                {"aweme_id": "1"},
                {"author": {}},
                {"author": {"nickname": "  第二个  "}},
            ]
        }
        self.assertEqual(douyin.parse_author_nickname(payload), "第二个")

    def test_never_invents_a_name_from_chrome_text(self):
        # A payload that only carries UI labels must yield nothing at all.
        self.assertEqual(douyin.parse_author_nickname({"aweme_list": []}), "")
        self.assertEqual(douyin.parse_author_nickname({"aweme_list": "nope"}), "")
        self.assertEqual(douyin.parse_author_nickname(None), "")
        self.assertEqual(douyin.parse_author_nickname({"aweme_list": [{"author": {"nickname": ""}}]}), "")


class ScrollBudgetTests(unittest.TestCase):
    """The scroll budget is derived from the comment target.

    Regression: the loop used a flat 60 scrolls of ~1.85s each = ~110s per work
    no matter how few comments were wanted.
    """

    def test_small_target_does_not_spin_for_a_minute(self):
        self.assertLessEqual(douyin.scroll_budget(20), 12)
        self.assertLessEqual(douyin.scroll_budget(60), 12)

    def test_budget_grows_with_the_target(self):
        self.assertGreater(douyin.scroll_budget(500), douyin.scroll_budget(100))

    def test_budget_is_clamped_at_both_ends(self):
        self.assertGreaterEqual(douyin.scroll_budget(0), 12)
        self.assertLessEqual(douyin.scroll_budget(10**9), 400)

    def test_budget_covers_the_pages_needed(self):
        # 20 comments per page, so 400 comments need 20 pages plus slack.
        self.assertGreaterEqual(douyin.scroll_budget(400), 20)


class AwaitResponseTests(unittest.TestCase):
    """`_await_response` must pump the event loop, not sleep blindly.

    With Playwright's sync API a plain `time.sleep` blocks delivery of the very
    responses we wait for, so the helper has to drive `wait_for_timeout`.
    """

    def _collector(self):
        return douyin.DouyinCollector(sleeper=lambda _s: None)

    def test_returns_true_as_soon_as_the_counter_moves(self):
        page = _FakePage()
        state = {"n": 0}

        # The counter ticks on the second pump: we must stop right there.
        def counter():
            state["n"] += 1
            return 1 if state["n"] > 2 else 0

        ok = self._collector()._await_response(page, counter, mark=0, timeout_ms=5000)
        self.assertTrue(ok)
        self.assertLess(page.pumped, 5, "should not keep waiting after data arrives")

    def test_times_out_when_nothing_ever_arrives(self):
        page = _FakePage()
        ok = self._collector()._await_response(page, lambda: 0, mark=0, timeout_ms=300)
        self.assertFalse(ok)
        self.assertGreater(page.pumped, 0, "must still pump the loop while waiting")

    def test_survives_a_page_that_closes_mid_wait(self):
        page = _FakePage(raise_on_pump=True)
        ok = self._collector()._await_response(page, lambda: 0, mark=0, timeout_ms=1000)
        self.assertFalse(ok)


class _FakePage:
    """Minimal stand-in for `page.wait_for_timeout`."""

    def __init__(self, raise_on_pump: bool = False) -> None:
        self.pumped = 0
        self._raise = raise_on_pump

    def wait_for_timeout(self, _ms: int) -> None:
        self.pumped += 1
        if self._raise:
            raise RuntimeError("Target closed")


class _FakeMouse:
    def __init__(self, page: "_ScriptedPage") -> None:
        self.page = page

    def move(self, _x: float, _y: float) -> None:
        self.page.moves += 1

    def wheel(self, _dx: float, _dy: float) -> None:
        self.page.wheeled += 1
        # Ask the scripted site for one more comment page on the next pump.
        self.page.pending += 1


class _ScriptedPage:
    """A fake Douyin video page that serves a fixed number of comment pages.

    `wait_for_timeout` is the only place events get delivered, exactly like the
    real Playwright sync API — which is what lets these tests prove the loop is
    driven by incoming data rather than by fixed sleeps.
    """

    def __init__(self, pages: int = 3, per_page: int = 20, report_bottom: bool = False) -> None:
        self.pages = pages
        self.per_page = per_page
        self.report_bottom = report_bottom
        self.served = 0
        self.pending = 0
        self.pumps = 0
        self.wheeled = 0
        self.moves = 0
        self._listeners = []
        self.mouse = _FakeMouse(self)

    # -- playwright surface ------------------------------------------------
    def on(self, _event: str, callback) -> None:
        self._listeners.append(callback)

    def remove_listener(self, _event: str, callback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def goto(self, _url: str, **_kwargs) -> None:
        self.pending += 1  # the page loads its first comment batch by itself

    def wait_for_timeout(self, _ms: int) -> None:
        self.pumps += 1
        if self.pending:
            self.pending -= 1
            for callback in list(self._listeners):
                callback(self)

    def evaluate(self, _js: str):
        # The at-bottom probe asks about scrollTop/clientHeight; the hover probe
        # does not. Report "pinned to the end" once the scripted pages run out.
        if "scrollTop" in _js and "clientHeight" in _js:
            return self.report_bottom and self.served >= self.pages
        return None

    def title(self) -> str:
        return "抖音 - 记录美好生活"

    def close(self) -> None:
        pass

    # -- the fake response the listener receives ---------------------------
    @property
    def url(self) -> str:
        return "https://www.douyin.com/aweme/v1/web/comment/list/?device_platform=webapp"

    def json(self):
        if self.served >= self.pages:
            # Panel exhausted: the real site simply stops sending payloads.
            return {"comments": [], "total": self.pages * self.per_page}
        start = self.served * self.per_page
        self.served += 1
        return {
            "total": self.pages * self.per_page,
            "comments": [
                {
                    "cid": str(start + i),
                    "text": f"评论{start + i}",
                    "digg_count": 1,
                    "create_time": 1700000000,
                    "user": {"nickname": f"用户{start + i}", "uid": str(start + i)},
                }
                for i in range(self.per_page)
            ],
        }


class CollectionLoopTests(unittest.TestCase):
    """The scroll loop must be data-driven, not sleep-driven.

    Regression: every tick slept a fixed 1.3-2.4s, so a 60-tick run cost ~110s
    per work even when the page answered in milliseconds, and the loop never
    noticed it already had everything the caller asked for.
    """

    def _collector_with(self, page: _ScriptedPage):
        collector = douyin.DouyinCollector(sleeper=lambda _s: None)
        collector._context = object()  # makes `running` true, skips the real launch
        collector._page = page
        return collector

    def test_stops_as_soon_as_the_target_is_reached(self):
        page = _ScriptedPage(pages=10, per_page=20)  # 200 comments available
        collector = self._collector_with(page)
        comments = collector.collect_comments("123", max_comments=40)

        self.assertEqual(len(comments), 40)
        # 40 comments = 2 pages; it must not paw through all 10.
        self.assertLessEqual(page.served, 3, "kept scrolling after the target was met")

    def test_exhausted_panel_ends_the_run_without_burning_the_budget(self):
        page = _ScriptedPage(pages=2, per_page=20)  # only 40 comments exist
        collector = self._collector_with(page)
        comments = collector.collect_comments("123", max_comments=500)

        self.assertEqual(len(comments), 40)
        # Budget for 500 comments is ~33 ticks; a stalled panel must cut it short.
        self.assertLess(
            page.wheeled,
            douyin.scroll_budget(500),
            "ran the full scroll budget against an empty panel",
        )

    def test_fast_page_does_not_wait_the_old_fixed_amount(self):
        page = _ScriptedPage(pages=2, per_page=20)
        collector = self._collector_with(page)
        collector.collect_comments("123", max_comments=20)

        # Each pump stands in for one POLL_STEP_MS. The old loop waited >=1.3s
        # per tick; here the whole run must stay in the low tens of pumps.
        self.assertLess(page.pumps, 60, "still waiting around instead of moving on")

    def test_video_with_no_comments_exits_promptly(self):
        page = _ScriptedPage(pages=0, per_page=20)
        collector = self._collector_with(page)
        comments = collector.collect_comments("123", max_comments=300)

        self.assertEqual(comments, [])
        # Four stalled ticks is the cap; it must not grind through 60.
        self.assertLess(page.wheeled, 12)

    def test_scroller_pinned_at_the_end_ends_the_run_at_once(self):
        # With a real bottom signal, one empty tick is enough. Without it the
        # loop waits out the whole stall budget (~4 x SCROLL_WAIT_MS) on every
        # fully-read video, which was pure dead time.
        page = _ScriptedPage(pages=2, per_page=20, report_bottom=True)
        collector = self._collector_with(page)
        comments = collector.collect_comments("123", max_comments=500)

        self.assertEqual(len(comments), 40)
        self.assertLessEqual(
            page.wheeled,
            page.pages + 1,
            "kept ticking after reaching the bottom of the comments",
        )

    def test_progress_callback_receives_count_and_title(self):
        page = _ScriptedPage(pages=2, per_page=20)
        collector = self._collector_with(page)
        seen: list[tuple[int, str]] = []
        collector.collect_comments(
            "123",
            max_comments=20,
            on_progress=lambda count, title: seen.append((count, title)),
        )

        self.assertTrue(seen, "progress was never reported")
        count, title = seen[0]
        self.assertIsInstance(count, int)
        self.assertIsInstance(title, str)


class CliProgressContractTests(unittest.TestCase):
    """The CLI's progress callback must match the collector's (count, title) shape.

    Regression: radar-collect.py handed over a dict-shaped callback, so the
    first real CLI collection died with "TypeError: _progress() takes 1
    positional argument but 2 were given" — hidden until now because the launch
    crash happened first.
    """

    def _load_cli(self):
        import importlib.util

        path = ROOT / "tools" / "radar-collect.py"
        spec = importlib.util.spec_from_file_location("radar_collect_cli", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_cli_progress_callback_accepts_count_and_title(self):
        import inspect

        module = self._load_cli()
        inspect.signature(module._comment_progress).bind(3, "某个作品")
        self.assertTrue(callable(module._comment_progress))

    def test_every_on_progress_callback_in_the_cli_has_the_right_arity(self):
        import inspect
        import re

        source = (ROOT / "tools" / "radar-collect.py").read_text(encoding="utf-8")
        module = self._load_cli()
        for name in re.findall(r"on_progress=(\w+)", source):
            callback = getattr(module, name)
            inspect.signature(callback).bind(1, "标题")


class LinkInputTests(unittest.TestCase):
    """Pasting a link is the other way in, and it has to survive the share sheet.

    Douyin hands out `/video/<id>`, `/note/<id>`, `?modal_id=<id>` and the
    `v.douyin.com` short form, and the app's share button wraps whichever one it
    picked in a sentence. All of them must land on the same numeric id.
    """

    ID = "7682414198638398729"

    def test_a_bare_id_is_accepted(self):
        self.assertEqual(douyin.parse_video_input(self.ID).ids, [self.ID])

    def test_the_plain_video_address_is_accepted(self):
        parsed = douyin.parse_video_input(f"https://www.douyin.com/video/{self.ID}")
        self.assertEqual(parsed.ids, [self.ID])
        self.assertEqual(parsed.share_links, [])

    def test_note_addresses_carry_the_same_id(self):
        self.assertEqual(douyin.parse_video_input(f"https://www.douyin.com/note/{self.ID}").ids, [self.ID])

    def test_query_style_addresses_are_accepted(self):
        for url in (
            f"https://www.douyin.com/video/{self.ID}?modal_id={self.ID}",
            f"https://www.douyin.com/?aweme_id={self.ID}",
        ):
            with self.subTest(url=url):
                self.assertEqual(douyin.parse_video_input(url).ids, [self.ID])

    def test_a_short_share_link_is_kept_for_resolution_not_guessed(self):
        parsed = douyin.parse_video_input("https://v.douyin.com/iRabcDe/")
        self.assertEqual(parsed.ids, [])
        self.assertEqual(parsed.share_links, ["https://v.douyin.com/iRabcDe/"])
        self.assertEqual(parsed.invalid, [])

    def test_a_share_address_that_already_carries_an_id_needs_no_round_trip(self):
        parsed = douyin.parse_video_input(f"https://www.iesdouyin.com/share/video/{self.ID}/")
        self.assertEqual(parsed.ids, [self.ID])
        self.assertEqual(parsed.share_links, [])

    def test_the_share_sheet_sentence_is_understood(self):
        text = (
            f"7.85 复制打开抖音，看看【某某某】的作品 我的新视频 "
            f"https://v.douyin.com/iRabcDe/ 05/07 Abc:/"
        )
        parsed = douyin.parse_video_input(text)
        self.assertEqual(parsed.share_links, ["https://v.douyin.com/iRabcDe/"])
        # 句子里的中文不是地址，不该被当成错误输入报出来
        self.assertEqual(parsed.invalid, [])

    def test_chinese_punctuation_right_after_the_url_is_stripped(self):
        parsed = douyin.parse_video_input(f"看这个 https://www.douyin.com/video/{self.ID}，很有意思")
        self.assertEqual(parsed.ids, [self.ID])

    def test_addresses_without_a_scheme_still_work(self):
        parsed = douyin.parse_video_input(f"www.douyin.com/video/{self.ID}")
        self.assertEqual(parsed.ids, [self.ID])

    def test_several_links_in_one_blob_are_all_picked_up(self):
        other = "7654321098765432109"
        text = f"https://www.douyin.com/video/{self.ID}\n{other}\nhttps://www.douyin.com/video/{self.ID}"
        parsed = douyin.parse_video_input(text)
        # 去重，且保持出现顺序
        self.assertEqual(parsed.ids, [self.ID, other])

    def test_a_profile_link_is_reported_as_invalid_rather_than_silently_dropped(self):
        parsed = douyin.parse_video_input("https://www.douyin.com/user/MS4wLjABAAAAxxxx")
        self.assertEqual(parsed.ids, [])
        self.assertEqual(len(parsed.invalid), 1)

    def test_empty_input_yields_nothing(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                parsed = douyin.parse_video_input(value)
                self.assertEqual((parsed.ids, parsed.share_links, parsed.invalid), ([], [], []))
                self.assertEqual(parsed.total, 0)

    def test_total_counts_both_kinds(self):
        parsed = douyin.parse_video_input(f"{self.ID}\nhttps://v.douyin.com/iRabcDe/")
        self.assertEqual(parsed.total, 2)
        self.assertEqual(parsed.as_dict()["total"], 2)


class VideoDetailTests(unittest.TestCase):
    """Link-sourced videos have no works entry, so the title comes from here."""

    def test_title_and_author_are_read_from_the_detail_payload(self):
        meta = douyin.parse_video_detail(
            {
                "aweme_detail": {
                    "aweme_id": "1234567890123456789",
                    "desc": "第一行标题\n第二行",
                    "create_time": 1700000000,
                    "author": {"nickname": "某位作者"},
                    "statistics": {"comment_count": 42, "digg_count": 777},
                }
            }
        )
        self.assertEqual(meta["awemeId"], "1234567890123456789")
        self.assertEqual(meta["title"], "第一行标题\n第二行")
        self.assertEqual(meta["author"], "某位作者")
        self.assertEqual(meta["commentCount"], 42)
        self.assertEqual(meta["diggCount"], 777)
        self.assertEqual(meta["createTime"], 1700000000)

    def test_a_payload_without_a_detail_is_ignored(self):
        for payload in ({}, {"aweme_detail": None}, [], "nope", None):
            with self.subTest(payload=payload):
                self.assertEqual(douyin.parse_video_detail(payload), {})

    def test_a_nested_data_wrapper_is_also_understood(self):
        meta = douyin.parse_video_detail({"data": {"aweme_id": "9", "desc": "标题"}})
        self.assertEqual(meta["title"], "标题")

    def test_missing_counters_default_to_zero_rather_than_blowing_up(self):
        meta = douyin.parse_video_detail({"aweme_detail": {"aweme_id": "9"}})
        self.assertEqual(meta["commentCount"], 0)
        self.assertEqual(meta["diggCount"], 0)
        self.assertEqual(meta["title"], "")
        self.assertEqual(meta["author"], "")


class PageTitleTests(unittest.TestCase):
    def test_the_brand_suffix_is_removed(self):
        self.assertEqual(douyin.clean_page_title("《我被五Der包围了》 #tag - 抖音"), "《我被五Der包围了》 #tag")

    def test_a_title_without_the_suffix_is_untouched(self):
        self.assertEqual(douyin.clean_page_title("普通标题"), "普通标题")

    def test_an_empty_title_stays_empty(self):
        self.assertEqual(douyin.clean_page_title(""), "")


class ServiceLinkResolutionTests(unittest.TestCase):
    """`_links` must not open a browser unless a short link actually needs it."""

    class _FakeCollector:
        def __init__(self, resolved=None):
            self.running = True
            self.resolved = resolved or {}
            self.resolve_calls = 0

        def resolve_share_links(self, links):
            self.resolve_calls += 1
            return {link: self.resolved[link] for link in links if link in self.resolved}

    def _service(self, resolved=None):
        from collector.service import CollectorService

        service = CollectorService(profile_dir="/tmp/radar-test-profile")
        fake = self._FakeCollector(resolved)
        service._collector = fake
        return service, fake

    def test_plain_links_never_touch_the_browser(self):
        service, fake = self._service()
        result = service._links(f"https://www.douyin.com/video/{'7' * 19}")
        self.assertEqual(result["ids"], ["7" * 19])
        self.assertEqual(fake.resolve_calls, 0)

    def test_a_short_link_is_resolved_by_the_browser(self):
        service, fake = self._service({"https://v.douyin.com/abc/": "7654321098765432109"})
        result = service._links("https://v.douyin.com/abc/")
        self.assertEqual(result["ids"], ["7654321098765432109"])
        self.assertEqual(result["resolved"]["https://v.douyin.com/abc/"], "7654321098765432109")
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(fake.resolve_calls, 1)

    def test_a_short_link_that_fails_to_resolve_is_reported_not_dropped(self):
        service, _fake = self._service()
        result = service._links("https://v.douyin.com/gone/")
        self.assertEqual(result["ids"], [])
        self.assertEqual(result["unresolved"], ["https://v.douyin.com/gone/"])
        self.assertEqual(result["total"], 0)

    def test_short_links_and_plain_ids_are_merged_without_duplicates(self):
        target = "7654321098765432109"
        service, _fake = self._service({"https://v.douyin.com/abc/": target})
        result = service._links(f"https://v.douyin.com/abc/\n{target}")
        self.assertEqual(result["ids"], [target])

    def test_the_links_operation_is_known_so_a_typo_cannot_launch_chrome(self):
        from collector.service import KNOWN_OPERATIONS

        self.assertIn("links", KNOWN_OPERATIONS)


class LinkSourcedRowsTests(unittest.TestCase):
    """Rows from a link must still say which video they came from."""

    class _FakeCollector:
        def __init__(self):
            self.running = True
            self.video_meta = {
                "7654321098765432109": {
                    "awemeId": "7654321098765432109",
                    "title": "别人的作品标题",
                    "author": "某位作者",
                    "commentCount": 42,
                    "diggCount": 7,
                    "createTime": 1700000000,
                }
            }

        def collect_comments(self, aweme_id, **kwargs):
            return [
                douyin.parse_comment(
                    {"cid": "c1", "text": "怎么买？", "user": {"nickname": "小林", "uid": "u1"}},
                    aweme_id=aweme_id,
                )
            ]

    def test_the_title_learned_from_the_detail_call_lands_in_the_csv(self):
        from collector.service import CollectorService

        service = CollectorService(profile_dir="/tmp/radar-test-profile")
        result = service._collect(
            self._FakeCollector(),
            {"awemeIds": ["7654321098765432109"], "works": [], "maxComments": 20},
            None,
        )
        self.assertEqual(result["count"], 1)
        row = result["rows"][0]
        columns = dict(zip(douyin.CSV_HEADER, row))
        self.assertEqual(columns["作品标题"], "别人的作品标题")
        self.assertIn("7654321098765432109", columns["作品链接"])


if __name__ == "__main__":
    unittest.main()
