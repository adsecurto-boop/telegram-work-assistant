"""Unit tests for the input normalization layer."""
import unittest

from nlp_normalizer import normalize_input


class InputNormalizerTests(unittest.TestCase):
    def test_whitespace_and_unicode_dashes_and_quotes(self):
        text = "  my   shift   tomorrow—is  from ‘10 am’ to ‘7 pm’  "
        norm = normalize_input(text)
        self.assertEqual(norm.raw_text, text)
        self.assertIn("10 am", norm.normalized_text)
        self.assertIn("7 pm", norm.normalized_text)
        self.assertNotIn("—", norm.normalized_text)
        self.assertNotIn("‘", norm.normalized_text)
        self.assertNotIn("’", norm.normalized_text)
        self.assertNotIn("   ", norm.normalized_text)

    def test_todays_and_tomorrow_variants(self):
        cases = [
            ("todays shift is 10 to 7", "today's shift is 10 to 7"),
            ("today s shift is 10 to 7", "today's shift is 10 to 7"),
            ("my shift tmrw is 8 to 5", "my shift tomorrow is 8 to 5"),
            ("shift for tomorow: 12 to 9", "shift for tomorrow: 12 to 9"),
            ("tommorrow i am working", "tomorrow i am working"),
        ]
        for inp, expected_sub in cases:
            with self.subTest(inp=inp):
                norm = normalize_input(inp)
                self.assertEqual(norm.normalized_text.casefold(), expected_sub.casefold())

    def test_time_range_dash_normalization(self):
        cases = [
            ("shift is 12-9", "shift is 12 to 9"),
            ("set shift 12–9 pm", "set shift 12 to 9 pm"),
            ("working 10am - 7pm", "working 10am to 7pm"),
            ("use 8:00 - 17:00", "use 8:00 to 17:00"),
        ]
        for inp, expected in cases:
            with self.subTest(inp=inp):
                norm = normalize_input(inp)
                self.assertEqual(norm.normalized_text.casefold(), expected.casefold())

    def test_protection_of_tickets_urls_emails_versions(self):
        text = 'Fixed BUG-42 in v4.2.1 on https://app.empmonitor.com for support@acme.com with error "connection-timeout"'
        norm = normalize_input(text)
        self.assertIn("BUG-42", norm.normalized_text)
        self.assertIn("v4.2.1", norm.normalized_text)
        self.assertIn("https://app.empmonitor.com", norm.normalized_text)
        self.assertIn("support@acme.com", norm.normalized_text)
        self.assertIn('"connection-timeout"', norm.normalized_text)

    def test_command_spacing_normalization(self):
        text = "/understand    my shift tomorrow is 10 to 7"
        norm = normalize_input(text)
        self.assertEqual(norm.normalized_text, "/understand my shift tomorrow is 10 to 7")

    def test_modal_and_uncertainty_detection(self):
        cases = [
            ("my shift tomorrow can be from 12 to 9 pm", ["can be"]),
            ("I might work from 8 to 5 tomorrow", ["might work"]),
            ("maybe shift is 10 to 7", ["maybe"]),
            ("it could be 12 to 9 pm", ["could be"]),
            ("we should be starting at 10", ["should be"]),
        ]
        for inp, expected_modals in cases:
            with self.subTest(inp=inp):
                norm = normalize_input(inp)
                for em in expected_modals:
                    self.assertIn(em, norm.detected_modals)

    def test_negation_detection(self):
        norm1 = normalize_input("do not change my shift to 10 to 7")
        self.assertTrue(norm1.has_negation)

        norm2 = normalize_input("don't mark task 3 complete")
        self.assertTrue(norm2.has_negation)

        norm3 = normalize_input("my shift tomorrow is 10 to 7")
        self.assertFalse(norm3.has_negation)


if __name__ == '__main__':
    unittest.main()
