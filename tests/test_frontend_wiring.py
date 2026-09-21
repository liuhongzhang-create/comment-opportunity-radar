"""Static checks that keep the front end wired to the back end.

These catch the class of mistake that no unit test sees: an element id that the
script looks up but the markup never defines, a script tag in the wrong order,
or a threshold key that exists in one language and not the other.
"""

import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
CORE_JS = (ROOT / "web" / "core.js").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")

SPEC = importlib.util.spec_from_file_location("radar_app", ROOT / "app.py")
APP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(APP)

# Classes that only ever come from JavaScript or from a template string.
JS_ONLY_CLASSES = {
    "ok",
    "bad",
    "is-dragging",
    "badge-error",
    "badge-high",
    "badge-medium",
    "badge-low",
    "badge-review",
}


def threshold_pairs(js: str) -> set[tuple[str, str]]:
    """Parse the THRESHOLD_FIELDS list as (backend key, dom id) pairs."""
    block = re.search(r"THRESHOLD_FIELDS\s*=\s*\[(.*?)\];", js, re.S)
    if not block:
        return set()
    return set(re.findall(r'\[\s*"([a-z_]+)"\s*,\s*"([a-z0-9-]+)"\s*\]', block.group(1)))


class ElementIdTests(unittest.TestCase):
    def setUp(self):
        self.html_ids = set(re.findall(r'id="([^"]+)"', INDEX))
        self.js_ids = set(re.findall(r'\$\("([^"]+)"\)', APP_JS))
        self.js_ids |= {dom_id for _key, dom_id in threshold_pairs(APP_JS)}

    def test_every_id_the_script_looks_up_exists_in_the_markup(self):
        missing = sorted(self.js_ids - self.html_ids)
        self.assertEqual(missing, [], f"app.js references missing element ids: {missing}")

    def test_the_field_list_is_actually_parsed(self):
        self.assertEqual(len(threshold_pairs(APP_JS)), len(APP.THRESHOLD_BOUNDS))

    def test_every_id_the_script_looks_up_exists_in_the_markup(self):
        missing = sorted(self.js_ids - self.html_ids)
        self.assertEqual(missing, [], f"app.js references missing element ids: {missing}")

    def test_threshold_inputs_are_all_wired(self):
        expected = {
            "thr-review",
            "thr-high-score",
            "thr-high-urgent",
            "thr-med-score",
            "thr-med-urgent",
        }
        self.assertTrue(expected <= self.html_ids)
        self.assertTrue(expected <= self.js_ids)

    def test_markup_has_the_progress_and_retry_controls(self):
        for element in ("progress-wrap", "progress-bar", "progress-label", "retry-button"):
            self.assertIn(element, self.html_ids)


class ScriptOrderTests(unittest.TestCase):
    def test_core_is_loaded_before_app(self):
        core_at = INDEX.index("/core.js")
        app_at = INDEX.index("/app.js")
        self.assertLess(core_at, app_at, "core.js must load first: app.js reads window.RadarCore")

    def test_both_scripts_are_deferred(self):
        for tag in re.findall(r"<script[^>]*>", INDEX):
            self.assertIn("defer", tag, tag)

    def test_app_js_consumes_the_shared_core(self):
        self.assertIn("window.RadarCore", APP_JS)
        self.assertIn("root.RadarCore", CORE_JS)

    def test_stylesheet_is_linked(self):
        self.assertIn('href="/styles.css"', INDEX)


class VisibilityAndOverlayTests(unittest.TestCase):
    """Regression guards found by driving the real page in a browser."""

    def test_the_hidden_attribute_beats_author_display_rules(self):
        # .file-controls / .summary-grid / .empty-state all set display:grid,
        # which overrides the user-agent rule for [hidden] and leaks an empty
        # dropdown plus four zeroed cards onto the first screen.
        self.assertRegex(CSS, r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important")

    def test_every_panel_toggled_with_hidden_is_actually_hideable(self):
        toggled = set(re.findall(r'\$\("([a-z-]+)"\)\.hidden\s*=', APP_JS))
        self.assertIn("summary-grid", toggled)
        self.assertIn("file-controls", toggled)
        self.assertIn("empty-state", toggled)

    def test_the_toast_cannot_swallow_button_clicks(self):
        toast = re.search(r"\.toast\s*\{([^}]*)\}", CSS)
        self.assertIsNotNone(toast, "missing .toast rule")
        self.assertIn("pointer-events: none", toast.group(1))


class BadgeStyleTests(unittest.TestCase):
    def test_every_badge_the_table_can_emit_has_a_style(self):
        for name in ("high", "medium", "low", "review", "error"):
            self.assertIn(f".badge-{name}", CSS, f"missing .badge-{name}")

    def test_classes_used_in_markup_are_styled(self):
        used = set()
        for attribute in re.findall(r'class="([^"]+)"', INDEX):
            used.update(attribute.split())
        missing = sorted(
            name for name in used if name not in JS_ONLY_CLASSES and f".{name}" not in CSS
        )
        self.assertEqual(missing, [], f"unstyled classes in index.html: {missing}")


class CrossLanguageContractTests(unittest.TestCase):
    def test_threshold_keys_match_the_backend(self):
        js_keys = {key for key, _dom_id in threshold_pairs(APP_JS)}
        self.assertEqual(js_keys, set(APP.THRESHOLD_BOUNDS))

    def test_priority_labels_match_the_backend(self):
        backend = {"高", "中", "低", "人工复核", "失败"}
        frontend = set(re.findall(r'"([高中低])"', APP_JS)) | {"人工复核", "失败"}
        self.assertTrue(backend <= frontend, f"front end cannot render: {backend - frontend}")

    def test_intent_labels_match_the_backend_criteria(self):
        backend = set(APP.QUESTIONS["intent"]["criteria"])
        frontend = set(re.findall(r'^\s{2}([a-z_]+): "', APP_JS, re.M))
        self.assertTrue(backend <= frontend, f"front end cannot label: {backend - frontend}")

    def test_export_headers_include_the_identity_columns_marker(self):
        self.assertIn("原CSV行号", CORE_JS)


if __name__ == "__main__":
    unittest.main()
