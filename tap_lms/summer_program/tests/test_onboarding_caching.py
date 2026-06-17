"""
CR-004 Slice 1 — AC-7: per-job reference caching in process_glific_contact.

Tests:
1. _ref() returns the same value as a direct frappe.get_value() call.
2. A second _ref() call for the same (doctype, name, fieldname) does NOT
   call frappe.get_value again (cache hit).
3. process_glific_contact accepts an optional ref_cache kwarg and populates
   it; re-calling with the same cache skips frappe.get_value for repeated
   lookups.
4. When ref_cache=None, process_glific_contact creates a local dict and
   still works (backward compat for existing direct callers).

No real DB or Glific network calls are made. All external interactions are
mocked.
"""
import unittest
from unittest.mock import patch, MagicMock, call

import tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process as bop


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_student_mock(school="SCH-001", language="LANG-001",
                       course_vertical="CV-001", grade="5",
                       phone="919876543210", name="BS-CACHE-001",
                       student_name="Cache Test Student", batch="BT-001"):
    bs = MagicMock()
    bs.name = name
    bs.student_name = student_name
    bs.phone = phone
    bs.school = school
    bs.language = language
    bs.course_vertical = course_vertical
    bs.grade = grade
    bs.batch = batch
    return bs


# ── Unit tests for _ref() ────────────────────────────────────────────────────

class TestRefHelper(unittest.TestCase):
    """Direct unit tests of the _ref() cache helper."""

    def test_ref_returns_correct_value(self):
        """_ref() must return the same value as frappe.get_value()."""
        cache = {}
        with patch.object(bop.frappe, "get_value", return_value="School Display Name"):
            result = bop._ref(cache, "School", "SCH-001", "name1")
        self.assertEqual(result, "School Display Name")

    def test_ref_caches_on_first_call(self):
        """After the first call, the value is in the cache."""
        cache = {}
        with patch.object(bop.frappe, "get_value", return_value="Cached Value"):
            bop._ref(cache, "School", "SCH-001", "name1")
        # Cache should contain the value under the expected key structure
        self.assertIn(("School", "name1"), cache)
        self.assertIn("SCH-001", cache[("School", "name1")])
        self.assertEqual(cache[("School", "name1")]["SCH-001"], "Cached Value")

    def test_ref_does_not_call_get_value_on_second_lookup(self):
        """AC-7 core assertion: second lookup for same key must NOT call
        frappe.get_value again."""
        cache = {}
        with patch.object(bop.frappe, "get_value", return_value="Val") as mock_gv:
            bop._ref(cache, "School", "SCH-001", "name1")  # first — DB hit
            bop._ref(cache, "School", "SCH-001", "name1")  # second — cache hit

        # get_value called exactly once despite two _ref() calls
        self.assertEqual(
            mock_gv.call_count, 1,
            f"frappe.get_value was called {mock_gv.call_count} times; "
            f"expected 1 (second call should be a cache hit)",
        )

    def test_ref_different_names_get_separate_entries(self):
        """Different document names for the same doctype/field get separate
        cache entries and each triggers one DB call."""
        cache = {}
        with patch.object(bop.frappe, "get_value",
                          side_effect=["Name A", "Name B"]) as mock_gv:
            val_a = bop._ref(cache, "School", "SCH-001", "name1")
            val_b = bop._ref(cache, "School", "SCH-002", "name1")

        self.assertEqual(val_a, "Name A")
        self.assertEqual(val_b, "Name B")
        self.assertEqual(mock_gv.call_count, 2)

    def test_ref_different_fields_get_separate_cache_keys(self):
        """Same doctype+name but different fieldnames are cached separately."""
        cache = {}
        with patch.object(bop.frappe, "get_value",
                          side_effect=["glific-lang-99", "hi"]) as mock_gv:
            gid = bop._ref(cache, "TAP Language", "Hindi", "glific_language_id")
            gcode = bop._ref(cache, "TAP Language", "Hindi", "glific_id")

        self.assertEqual(gid, "glific-lang-99")
        self.assertEqual(gcode, "hi")
        self.assertEqual(mock_gv.call_count, 2)

    def test_ref_returns_none_when_get_value_returns_none(self):
        """_ref() must pass through None without erroring."""
        cache = {}
        with patch.object(bop.frappe, "get_value", return_value=None):
            result = bop._ref(cache, "School", "NONEXISTENT", "name1")
        self.assertIsNone(result)

    def test_ref_caches_none_value(self):
        """A None result is cached; second call must not re-query the DB."""
        cache = {}
        with patch.object(bop.frappe, "get_value", return_value=None) as mock_gv:
            bop._ref(cache, "School", "GHOST", "name1")
            bop._ref(cache, "School", "GHOST", "name1")
        self.assertEqual(mock_gv.call_count, 1)


# ── Integration tests for process_glific_contact ref_cache param ─────────────

class TestProcessGlificContactCaching(unittest.TestCase):
    """Verify that process_glific_contact uses the ref_cache for the four
    reference-doctype lookups (School, TAP Language, Course Verticals,
    Course Level)."""

    def _run_process_glific_contact(self, ref_cache, course_level="CL-001",
                                    contact_return=None):
        """Call process_glific_contact with mocked Glific and DB layers.

        Returns (get_value_calls_count, result).
        """
        student = _make_student_mock()

        # For the Batch.name lookup (not currently cached — just a get_value)
        # and the four cached lookups, we need to track call counts.
        # We only care about calls that SHOULD be cached.
        cached_doctypes = {"School", "TAP Language", "Course Level", "Course Verticals"}

        call_log = []

        def fake_get_value(doctype, name=None, fieldname=None, *args, **kwargs):
            call_log.append((doctype, name, fieldname))
            mapping = {
                ("School",          "SCH-001",  "name1"):                  "School Display",
                ("TAP Language",    "LANG-001", "glific_language_id"):     "LANG-99",
                ("Course Level",    "CL-001",   "name1"):                  "Level 1",
                ("Course Verticals","CV-001",   "name2"):                  "Coding",
                ("Batch",           "BT-001",   "name"):                   "BT-001",
            }
            return mapping.get((doctype, name, fieldname))

        with patch.object(bop.frappe, "get_value", side_effect=fake_get_value), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.format_phone_number",
                   return_value="919876543210"), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.get_contact_by_phone",
                   return_value=contact_return or {}), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.add_student_to_glific_for_onboarding",
                   return_value={"id": "GLIFIC-42"}):

            result = bop.process_glific_contact(
                student, None, course_level, ref_cache
            )

        cached_calls = [(d, n, f) for d, n, f in call_log if d in cached_doctypes]
        return cached_calls, result

    def test_ref_cache_populated_after_first_call(self):
        """After the first process_glific_contact call, the shared cache
        contains entries for School, TAP Language, Course Level,
        Course Verticals."""
        ref_cache = {}
        cached_calls, _ = self._run_process_glific_contact(ref_cache)

        # All four cached doctypes should have been queried exactly once
        doctypes_called = {d for d, n, f in cached_calls}
        self.assertIn("School", doctypes_called)
        self.assertIn("TAP Language", doctypes_called)
        self.assertIn("Course Level", doctypes_called)
        self.assertIn("Course Verticals", doctypes_called)

        # And the results are now cached
        self.assertIn(("School", "name1"), ref_cache)
        self.assertIn(("TAP Language", "glific_language_id"), ref_cache)

    def test_second_call_with_same_cache_skips_db(self):
        """AC-7: a second process_glific_contact call for the same student
        references (same school / language / course_level / vertical) must
        NOT call frappe.get_value for the cached fields again."""
        ref_cache = {}

        # First call — populates cache
        first_calls, _ = self._run_process_glific_contact(ref_cache)

        # Second call — same student data, same ref_cache
        second_calls, _ = self._run_process_glific_contact(ref_cache)

        # The cached doctypes must NOT appear in the second call's get_value log
        second_cached = {d for d, n, f in second_calls}
        self.assertNotIn(
            "School", second_cached,
            "School should be a cache hit on the second call — frappe.get_value should not be called",
        )
        self.assertNotIn(
            "TAP Language", second_cached,
            "TAP Language should be a cache hit on the second call",
        )
        self.assertNotIn(
            "Course Level", second_cached,
            "Course Level should be a cache hit on the second call",
        )
        self.assertNotIn(
            "Course Verticals", second_cached,
            "Course Verticals should be a cache hit on the second call",
        )

    def test_none_ref_cache_creates_local_dict(self):
        """Backward compat: when ref_cache=None, process_glific_contact must
        create a local dict and work correctly — no TypeError, correct return."""
        student = _make_student_mock()

        with patch.object(bop.frappe, "get_value", return_value="SomeValue"), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.format_phone_number",
                   return_value="919876543210"), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.get_contact_by_phone",
                   return_value={}), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.add_student_to_glific_for_onboarding",
                   return_value={"id": "GLIFIC-99"}):
            try:
                result = bop.process_glific_contact(student, None, "CL-001",
                                                    ref_cache=None)
            except Exception as exc:
                self.fail(
                    f"process_glific_contact raised with ref_cache=None: {exc}"
                )
        # Should return the new contact dict
        self.assertEqual(result, {"id": "GLIFIC-99"})

    def test_cached_value_equals_direct_get_value(self):
        """AC-7 direct assertion: value returned via _ref() with a warm cache
        equals the value frappe.get_value would have returned directly."""
        cache = {}
        expected = "My School Name"

        with patch.object(bop.frappe, "get_value", return_value=expected):
            # First call populates cache
            val1 = bop._ref(cache, "School", "SCH-DIRECT", "name1")

        # Second call reads from cache (no DB)
        with patch.object(bop.frappe, "get_value",
                          side_effect=AssertionError("frappe.get_value must not be called on cache hit")):
            val2 = bop._ref(cache, "School", "SCH-DIRECT", "name1")

        self.assertEqual(val1, expected)
        self.assertEqual(val2, expected, "Cached value must equal direct get_value result")


if __name__ == "__main__":
    unittest.main()
