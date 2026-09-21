import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("radar_app", MODULE_PATH)
APP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(APP)


class PriorityTests(unittest.TestCase):
    def test_low_confidence_requires_review(self):
        self.assertEqual(APP.priority_for("purchase", 4, 1, 0.2), "人工复核")

    def test_purchase_with_strong_signal_is_high(self):
        self.assertEqual(APP.priority_for("purchase", 3.2, 0.7, 0.9), "高")

    def test_content_request_is_medium(self):
        self.assertEqual(APP.priority_for("content_request", 0.5, 0.1, 0.8), "中")

    def test_casual_comment_is_low(self):
        self.assertEqual(APP.priority_for("casual", 0.1, 0.1, 0.8), "低")

    def test_probability_is_clamped(self):
        self.assertEqual(APP.clamp_probability(2), 1.0)
        self.assertEqual(APP.clamp_probability(-1), 0.0)
        self.assertEqual(APP.clamp_probability("bad"), 0.0)


if __name__ == "__main__":
    unittest.main()
