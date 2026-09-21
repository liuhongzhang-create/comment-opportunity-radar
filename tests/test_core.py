"""Unit and contract tests for the scoring core.

Run with: python3 -m unittest discover -s tests -v
No network access and no API key are required.
"""

import importlib.util
import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("radar_app", MODULE_PATH)
APP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(APP)


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# Verbatim shape from https://docs.typesafe.ai/api#example-response, with all
# three question types present so the whole parser is exercised.
OFFICIAL_RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {
        "intent": {
            "type": "choice",
            "choice": "purchase",
            "probabilities": {
                "purchase": 0.8,
                "objection": 0.1,
                "content_request": 0.05,
                "complaint": 0.03,
                "casual": 0.02,
            },
            "confidence": 0.75,
        },
        "purchase_intent": {
            "type": "score",
            "score": 1.05,
            "legend": {"0": "none", "1": "mild"},
            "probabilities": {"0": 0.05, "1": 0.9, "2": 0.05},
            "confidence": 0.92,
        },
        "needs_fast_reply": {"type": "noul", "noul": 0.95},
    },
    "usage": {"input_tokens": 296, "output_tokens": 20},
}


class ThresholdTests(unittest.TestCase):
    def test_low_confidence_requires_review(self):
        self.assertEqual(APP.priority_for("purchase", 4, 1, 0.2), "人工复核")

    def test_purchase_with_strong_signal_is_high(self):
        self.assertEqual(APP.priority_for("purchase", 3.2, 0.7, 0.9), "高")

    def test_content_request_is_medium(self):
        self.assertEqual(APP.priority_for("content_request", 0.5, 0.1, 0.8), "中")

    def test_casual_comment_is_low(self):
        self.assertEqual(APP.priority_for("casual", 0.1, 0.1, 0.8), "低")

    def test_review_gate_can_be_retuned(self):
        """The same row flips once the caller moves the confidence floor."""
        strict = {"review_confidence": 0.9}
        loose = {"review_confidence": 0.1}
        self.assertEqual(APP.priority_for("purchase", 3.2, 0.7, 0.5, strict), "人工复核")
        self.assertEqual(APP.priority_for("purchase", 3.2, 0.7, 0.5, loose), "高")

    def test_high_thresholds_are_respected(self):
        tuned = {"high_purchase_score": 3.9, "high_urgent": 0.99}
        self.assertEqual(APP.priority_for("purchase", 3.2, 0.7, 0.9, tuned), "中")

    def test_partial_threshold_dict_keeps_other_defaults(self):
        result = APP.priority_for("casual", 0.1, 0.1, 0.8, {"review_confidence": 0.9})
        self.assertEqual(result, "人工复核")


class ThresholdResolutionTests(unittest.TestCase):
    def test_probability_is_clamped(self):
        self.assertEqual(APP.clamp_probability(2), 1.0)
        self.assertEqual(APP.clamp_probability(-1), 0.0)
        self.assertEqual(APP.clamp_probability("bad"), 0.0)
        self.assertEqual(APP.clamp_probability(None), 0.0)

    def test_out_of_range_values_are_clamped(self):
        resolved = APP.resolve_thresholds({"review_confidence": 5, "high_urgent": -3})
        self.assertEqual(resolved["review_confidence"], 1.0)
        self.assertEqual(resolved["high_urgent"], 0.0)

    def test_junk_values_fall_back_to_defaults(self):
        resolved = APP.resolve_thresholds(
            {"review_confidence": "abc", "high_purchase_score": None, "unknown": 1}
        )
        self.assertEqual(resolved["review_confidence"], APP.DEFAULT_THRESHOLDS["review_confidence"])
        self.assertEqual(
            resolved["high_purchase_score"], APP.DEFAULT_THRESHOLDS["high_purchase_score"]
        )
        self.assertNotIn("unknown", resolved)

    def test_nan_is_ignored(self):
        resolved = APP.resolve_thresholds({"review_confidence": float("nan")})
        self.assertEqual(resolved["review_confidence"], APP.DEFAULT_THRESHOLDS["review_confidence"])


class OptionsTests(unittest.TestCase):
    def test_defaults_when_options_missing(self):
        options = APP.resolve_options(None)
        self.assertEqual(options["model"], APP.DEFAULT_MODEL)
        self.assertEqual(options["concurrency"], APP.DEFAULT_CONCURRENCY)

    def test_model_is_pinned_to_a_version_not_an_alias(self):
        self.assertNotIn(APP.DEFAULT_MODEL, APP.KNOWN_ALIASES)
        self.assertRegex(APP.DEFAULT_MODEL, r"^jev-\d+\.\d+\.\d+$")

    def test_illegal_model_name_is_rejected(self):
        for bad in ["bad model!", "a" * 80, "../../etc/passwd", "-leading-dash"]:
            with self.subTest(model=bad):
                with self.assertRaises(ValueError):
                    APP.resolve_options({"model": bad})

    def test_blank_model_falls_back_to_the_default(self):
        self.assertEqual(APP.resolve_options({"model": "   "})["model"], APP.DEFAULT_MODEL)

    def test_concurrency_is_clamped(self):
        self.assertEqual(APP.resolve_options({"concurrency": 999})["concurrency"], APP.MAX_CONCURRENCY)
        self.assertEqual(APP.resolve_options({"concurrency": 0})["concurrency"], 1)
        self.assertEqual(APP.resolve_options({"concurrency": "x"})["concurrency"], APP.DEFAULT_CONCURRENCY)


class RowNormalizationTests(unittest.TestCase):
    def test_legacy_string_rows_still_work(self):
        rows = APP.normalize_rows(["a", "b"])
        self.assertEqual([row["text"] for row in rows], ["a", "b"])
        self.assertEqual([row["source_index"] for row in rows], [0, 1])

    def test_object_rows_keep_their_source_index(self):
        rows = APP.normalize_rows([{"text": "hi", "source_index": 17}])
        self.assertEqual(rows[0]["source_index"], 17)
        self.assertEqual(rows[0]["index"], 0)

    def test_blank_rows_are_rejected(self):
        with self.assertRaises(ValueError):
            APP.normalize_rows(["ok", "   "])

    def test_empty_and_oversized_input_is_rejected(self):
        with self.assertRaises(ValueError):
            APP.normalize_rows([])
        with self.assertRaises(ValueError):
            APP.normalize_rows(["x"] * (APP.MAX_ROWS + 1))

    def test_bad_source_index_falls_back_to_position(self):
        rows = APP.normalize_rows([{"text": "hi", "source_index": "not-a-number"}])
        self.assertEqual(rows[0]["source_index"], 0)

    def test_long_text_is_truncated(self):
        rows = APP.normalize_rows(["字" * 20000])
        self.assertEqual(len(rows[0]["text"]), APP.MAX_TEXT_CHARS)


class NormalizeResultTests(unittest.TestCase):
    """Contract tests against the response shape published at docs.typesafe.ai/api."""

    RESPONSE = OFFICIAL_RESPONSE

    def setUp(self):
        self.row = {"index": 0, "source_index": 4, "text": "怎么买？"}

    def test_official_response_shape_is_parsed(self):
        result = APP.normalize_result(self.row, self.RESPONSE)
        self.assertEqual(result["intent"], "purchase")
        self.assertEqual(result["intentConfidence"], 0.75)
        self.assertEqual(result["purchaseScore"], 1.05)
        self.assertEqual(result["urgentProbability"], 0.95)
        self.assertEqual(result["model"], "jev-1.13.0")
        self.assertEqual(result["source_index"], 4)

    def test_probabilities_survive_into_the_result(self):
        result = APP.normalize_result(self.row, self.RESPONSE)
        self.assertEqual(result["probabilities"]["purchase"], 0.8)

    def test_score_may_land_between_levels(self):
        result = APP.normalize_result(self.row, self.RESPONSE)
        self.assertAlmostEqual(result["purchaseScore"], 1.05, places=2)

    def test_missing_answer_blocks_do_not_crash(self):
        result = APP.normalize_result(self.row, {})
        self.assertEqual(result["intent"], "unknown")
        self.assertEqual(result["intentConfidence"], 0.0)
        self.assertEqual(result["purchaseScore"], 0.0)
        self.assertEqual(result["urgentProbability"], 0.0)
        self.assertEqual(result["priority"], "人工复核")

    def test_malformed_score_is_coerced(self):
        response = json.loads(json.dumps(self.RESPONSE))
        response["answers"]["purchase_intent"]["score"] = "nonsense"
        result = APP.normalize_result(self.row, response)
        self.assertEqual(result["purchaseScore"], 0.0)

    def test_out_of_range_probabilities_are_clamped(self):
        response = json.loads(json.dumps(self.RESPONSE))
        response["answers"]["intent"]["confidence"] = 7
        response["answers"]["needs_fast_reply"]["noul"] = -2
        result = APP.normalize_result(self.row, response)
        self.assertEqual(result["intentConfidence"], 1.0)
        self.assertEqual(result["urgentProbability"], 0.0)

    def test_probabilities_of_wrong_type_do_not_leak(self):
        response = json.loads(json.dumps(self.RESPONSE))
        response["answers"]["intent"]["probabilities"] = "oops"
        result = APP.normalize_result(self.row, response)
        self.assertEqual(result["probabilities"], {})


class RequestContractTests(unittest.TestCase):
    """The request body must match the published schema exactly."""

    def test_question_types_are_legal(self):
        for name, question in APP.QUESTIONS.items():
            with self.subTest(question=name):
                self.assertIn(question["type"], {"choice", "score", "noul"})
                self.assertIn("instructions", question)

    def test_choice_criteria_is_a_map(self):
        criteria = APP.QUESTIONS["intent"]["criteria"]
        self.assertIsInstance(criteria, dict)
        self.assertLessEqual(len(criteria), 255)
        for value in criteria.values():
            self.assertIsInstance(value, str)

    def test_score_criteria_is_an_ordered_array_of_two_to_ten(self):
        criteria = APP.QUESTIONS["purchase_intent"]["criteria"]
        self.assertIsInstance(criteria, list)
        self.assertGreaterEqual(len(criteria), 2)
        self.assertLessEqual(len(criteria), 10)

    def test_noul_answers_do_not_promise_confidence(self):
        """Official docs: only Choice and Score answers carry confidence."""
        self.assertEqual(APP.QUESTIONS["needs_fast_reply"]["type"], "noul")
        result = APP.normalize_result(
            {"index": 0, "source_index": 0, "text": "x"},
            {"answers": {"needs_fast_reply": {"type": "noul", "noul": 0.4}}},
        )
        # The urgency path must not read a confidence that is never sent.
        self.assertEqual(result["urgentProbability"], 0.4)

    def test_call_typesafe_sends_the_documented_body(self):
        captured = {}

        def fake_open(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            return FakeResponse(OFFICIAL_RESPONSE)

        with mock.patch.object(APP, "OPENER") as opener:
            opener.open.side_effect = fake_open
            result = APP.call_typesafe("secret-key", "怎么买？", "jev-1.13.0")

        self.assertEqual(captured["url"], APP.EVAL_URL)
        self.assertEqual(captured["body"]["state"], "怎么买？")
        self.assertEqual(captured["body"]["model"], "jev-1.13.0")
        self.assertEqual(captured["body"]["questions"], APP.QUESTIONS)
        self.assertEqual(captured["headers"]["authorization"], "Bearer secret-key")
        self.assertEqual(captured["headers"]["content-type"], "application/json")
        self.assertEqual(result["model"], "jev-1.13.0")


class RetryPolicyTests(unittest.TestCase):
    def test_rate_limit_and_server_errors_are_retryable(self):
        for status in (429, 529, 500, 502, 503, 504):
            self.assertTrue(APP._retryable_status(status), status)

    def test_caller_errors_are_not_retryable(self):
        for status in (400, 401, 403, 404, 413, 422):
            self.assertFalse(APP._retryable_status(status), status)

    def test_retry_after_header_wins(self):
        self.assertEqual(APP._backoff_seconds(0, "7"), 7.0)
        self.assertEqual(APP._backoff_seconds(0, "999"), 30.0)

    def test_backoff_grows_and_stays_bounded(self):
        first = APP._backoff_seconds(0, None)
        third = APP._backoff_seconds(3, None)
        self.assertGreaterEqual(first, 1.0)
        self.assertLess(first, 1.5)
        self.assertLessEqual(third, 30.0)
        self.assertGreater(third, first)

    def test_non_retryable_http_error_surfaces_the_api_detail(self):
        error = urllib.error.HTTPError(APP.EVAL_URL, 422, "Unprocessable", {}, None)
        error.read = lambda: json.dumps({"detail": "questions.intent: field required"}).encode()
        with mock.patch.object(APP, "OPENER") as opener:
            opener.open.side_effect = error
            with self.assertRaises(RuntimeError) as ctx:
                APP.call_typesafe("k", "text", "jev-1.13.0")
        self.assertIn("422", str(ctx.exception))
        self.assertIn("questions.intent", str(ctx.exception))
        self.assertEqual(opener.open.call_count, 1, "422 must not be retried")


class ErrorMessageTests(unittest.TestCase):
    def test_json_detail_is_extracted(self):
        self.assertEqual(APP._error_message('{"detail": "boom"}'), "boom")

    def test_error_and_message_keys_are_supported(self):
        self.assertEqual(APP._error_message('{"error": "nope"}'), "nope")
        self.assertEqual(APP._error_message('{"message": "hmm"}'), "hmm")

    def test_validation_detail_list_is_serialised(self):
        body = json.dumps({"detail": [{"loc": ["body", "model"], "msg": "invalid"}]})
        self.assertIn("invalid", APP._error_message(body))

    def test_plain_text_body_is_passed_through(self):
        self.assertEqual(APP._error_message("  gateway timeout  "), "gateway timeout")

    def test_empty_body_gets_a_readable_placeholder(self):
        self.assertEqual(APP._error_message(""), "（无响应内容）")


if __name__ == "__main__":
    unittest.main()
