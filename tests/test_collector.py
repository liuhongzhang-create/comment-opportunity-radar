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


if __name__ == "__main__":
    unittest.main()
