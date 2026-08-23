"""
test_blood_group_engine.py
--------------------------
Unit tests for the deterministic blood-group extraction core
(validation, normalization, text scanning, VLM reply parsing, and the
never-guess decision logic).

Run with:  python -m unittest test_blood_group_engine -v
(or:       python test_blood_group_engine.py)
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from blood_group_engine import (  # noqa: E402
    BLOOD_GROUPS, VALID_BLOOD_GROUPS,
    is_valid_blood_group, normalize_blood_group,
    scan_text_for_blood_groups, parse_vlm_reply, decide_extraction,
    name_similar,
)


class ValidationTest(unittest.TestCase):
    def test_all_real_groups_valid(self):
        for group in BLOOD_GROUPS:
            self.assertTrue(is_valid_blood_group(group), group)
            self.assertIn(group, VALID_BLOOD_GROUPS)

    def test_messy_but_valid_values_normalize(self):
        cases = {
            "b+": "B+", "B +": "B+", "b positive": "B+",
            "o-": "O-", "O negative": "O-", "o neg": "O-",
            "ab+": "AB+", "A B +": "AB+", "AB POS": "AB+",
            "a-": "A-",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_blood_group(raw), expected, raw)

    def test_invalid_values_rejected(self):
        for bad in ("", None, 0, 1.5, "B", "A0", "0+", "AB", "B++",
                    "O-zero", "Type A", "ABO", "B+P", "random", "B-extra"):
            self.assertFalse(is_valid_blood_group(bad), repr(bad))
            self.assertIsNone(normalize_blood_group(bad), repr(bad))

    def test_rh_variant_forms_scanned(self):
        # "B RhD Positive" and "A (II) Rh+" are readable by the text scanner.
        self.assertEqual(scan_text_for_blood_groups("B RhD Positive"), {"B+"})
        self.assertEqual(scan_text_for_blood_groups("A (II) Rh+"), {"A+"})


class ScanTextTest(unittest.TestCase):
    def test_finds_groups_in_prose(self):
        text = "The patient is O+ and the donor is B-. Ready to donate."
        groups = scan_text_for_blood_groups(text)
        self.assertEqual(groups, {"O+", "B-"})

    def test_european_roman_numeral_format(self):
        text = "Blutgruppe A (II) Rh+ Kontrollnummer 42"
        self.assertEqual(scan_text_for_blood_groups(text), {"A+"})

    def test_word_forms(self):
        text = "A positive or O negative donors preferred."
        self.assertEqual(scan_text_for_blood_groups(text), {"A+", "O-"})

    def test_zero_is_not_a_group(self):
        self.assertEqual(scan_text_for_blood_groups("0+"), set())
        self.assertEqual(scan_text_for_blood_groups("O plus"), set())

    def test_standalone_letter_not_a_group(self):
        self.assertEqual(scan_text_for_blood_groups("Just B and O"), set())

    def test_empty_and_none(self):
        self.assertEqual(scan_text_for_blood_groups(""), set())
        self.assertEqual(scan_text_for_blood_groups(None), set())

    def test_conflict_detection_across_sentences(self):
        text = "Card says AB+. Another panel lists O-."
        self.assertEqual(scan_text_for_blood_groups(text), {"AB+", "O-"})


class ParseVlmReplyTest(unittest.TestCase):
    def test_clean_json(self):
        reply = ('{"found": true, "blood_group": "B+", "blood_groups": ["B+"], '
                 '"confidence": 0.92, "card_holder_name": "Rahim Ahmed", '
                 '"card_id": "DNR-4471"}')
        parsed = parse_vlm_reply(reply)
        self.assertTrue(parsed["found"])
        self.assertTrue(parsed["json_parsed"])
        self.assertEqual(parsed["blood_group"], "B+")
        self.assertEqual(parsed["candidates"], {"B+"})
        self.assertAlmostEqual(parsed["confidence"], 0.92)
        self.assertEqual(parsed["card_holder_name"], "Rahim Ahmed")
        self.assertEqual(parsed["card_id"], "DNR-4471")

    def test_confidence_percent_scaled_to_zero_one(self):
        parsed = parse_vlm_reply('{"found": true, "blood_group": "O-", '
                                 '"confidence": 85}')
        self.assertAlmostEqual(parsed["confidence"], 0.85)

    def test_lists_and_aliases(self):
        parsed = parse_vlm_reply('{"found": true, "blood_groups": ["A+", "O+"], '
                                 '"name": "Karim", "donor_id": "K-9"}')
        self.assertEqual(parsed["candidates"], {"A+", "O+"})
        self.assertIsNone(parsed["blood_group"])  # conflict -> no single group
        self.assertEqual(parsed["card_holder_name"], "Karim")
        self.assertEqual(parsed["card_id"], "K-9")

    def test_malformed_json_falls_back_to_text_scan(self):
        reply = "Sure! The blood group is AB+ printed under the QR code."
        parsed = parse_vlm_reply(reply)
        self.assertFalse(parsed["json_parsed"])
        self.assertTrue(parsed["found"])
        self.assertEqual(parsed["blood_group"], "AB+")
        self.assertEqual(parsed["candidates"], {"AB+"})
        self.assertIsNone(parsed["confidence"])

    def test_empty_reply(self):
        parsed = parse_vlm_reply("")
        self.assertFalse(parsed["found"])
        self.assertEqual(parsed["candidates"], set())
        self.assertIsNone(parsed["blood_group"])

    def test_explicit_found_false_beats_text(self):
        # The model clearly said nothing was found; the text scan has nothing.
        parsed = parse_vlm_reply('{"found": false, "blood_group": null, '
                                 '"confidence": null}')
        self.assertFalse(parsed["found"])


class DecideExtractionTest(unittest.TestCase):
    def test_ok_when_single_confident_group(self):
        parsed = parse_vlm_reply('{"found": true, "blood_group": "B+", '
                                 '"confidence": 0.9}')
        decision = decide_extraction(parsed)
        self.assertEqual(decision["status"], "ok")
        self.assertEqual(decision["blood_group"], "B+")

    def test_no_group_is_verification_required(self):
        decision = decide_extraction(parse_vlm_reply('{"found": false}'))
        self.assertEqual(decision["status"], "verification_required")
        self.assertIsNone(decision["blood_group"])
        self.assertIsNotNone(decision["reason"])

    def test_conflict_is_verification_required(self):
        parsed = parse_vlm_reply('{"found": true, "blood_groups": ["A+", "B+"], '
                                 '"confidence": 0.9}')
        decision = decide_extraction(parsed)
        self.assertEqual(decision["status"], "verification_required")
        self.assertIn("Conflicting", decision["reason"])

    def test_low_confidence_is_verification_required(self):
        parsed = parse_vlm_reply('{"found": true, "blood_group": "O+", '
                                 '"confidence": 0.4}')
        decision = decide_extraction(parsed)
        self.assertEqual(decision["status"], "verification_required")
        self.assertEqual(decision["blood_group"], "O+")
        self.assertIn("Low confidence", decision["reason"])

    def test_threshold_is_configurable(self):
        parsed = parse_vlm_reply('{"found": true, "blood_group": "O+", '
                                 '"confidence": 0.7}')
        self.assertEqual(decide_extraction(parsed)["status"], "ok")
        self.assertEqual(
            decide_extraction(parsed, confidence_threshold=0.8)["status"],
            "verification_required",
        )

    def test_missing_confidence_is_verification_required(self):
        parsed = parse_vlm_reply('{"found": true, "blood_group": "A-"}')
        decision = decide_extraction(parsed)
        self.assertEqual(decision["status"], "verification_required")


class NameSimilarTest(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(name_similar("Rahim Ahmed", "Rahim Ahmed"))

    def test_substring_match(self):
        self.assertTrue(name_similar("RAHIM AHMED", "Rahim"))
        self.assertTrue(name_similar("Md. Rahim Ahmed", "Rahim Ahmed"))

    def test_first_token_match(self):
        self.assertTrue(name_similar("Karim Uddin", "Karim Hossain"))

    def test_mismatch(self):
        self.assertFalse(name_similar("Sakib Hasan", "Rahim Ahmed"))

    def test_missing_names(self):
        self.assertFalse(name_similar("", "Rahim Ahmed"))
        self.assertFalse(name_similar(None, "Rahim Ahmed"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
