"""
test_donor_import_engine.py
---------------------------
Unit tests for the admin bulk donor-import parser/validator.

Run with:  python -m unittest test_donor_import_engine -v
(or:       python test_donor_import_engine.py)
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from donor_import_engine import (  # noqa: E402
    clean_phone, normalize_ocr_group, is_noise_line,
    parse_freeform, parse_csv, parse_donor_text,
    record_errors, record_email, record_primary_phone,
    DEFAULT_IMPORT_PASSWORD,
)


class CleanPhoneTest(unittest.TestCase):
    def test_clean_formats(self):
        self.assertEqual(clean_phone("01710-027589"), "01710027589")
        self.assertEqual(clean_phone("01710 027589"), "01710027589")
        self.assertEqual(clean_phone("01710027589"), "01710027589")
        self.assertEqual(clean_phone("01747908487"), "01747908487")

    def test_bangladesh_prefix_stripped(self):
        self.assertEqual(clean_phone("8801710027589"), "01710027589")
        self.assertEqual(clean_phone("8801710027589" + ""), "01710027589")

    def test_invalid(self):
        self.assertIsNone(clean_phone("01521-20432!"))  # only 10 digits
        self.assertIsNone(clean_phone("12345"))
        self.assertIsNone(clean_phone("01710027589x1"))  # 12 digits
        self.assertIsNone(clean_phone(None))
        self.assertIsNone(clean_phone(""))


class NormalizeOcrGroupTest(unittest.TestCase):
    def test_ocr_zero_becomes_o(self):
        self.assertEqual(normalize_ocr_group("0+"), "O+")
        self.assertEqual(normalize_ocr_group("0-"), "O-")
        self.assertEqual(normalize_ocr_group("0 +"), "O+")

    def test_word_forms(self):
        self.assertEqual(normalize_ocr_group("o neg"), "O-")
        self.assertEqual(normalize_ocr_group("B positive"), "B+")

    def test_spaces_and_roman(self):
        self.assertEqual(normalize_ocr_group("B -"), "B-")
        self.assertEqual(normalize_ocr_group("A (II) Rh+"), "A+")
        self.assertEqual(normalize_ocr_group("AB+"), "AB+")

    def test_rejects_non_groups(self):
        for bad in ("A", "B", "O", "AB", "0", "Mr.", "Md.", "Asad",
                    "01710027589", "ABO"):
            self.assertIsNone(normalize_ocr_group(bad), bad)


class NoiseLineTest(unittest.TestCase):
    def test_footers_and_markers(self):
        self.assertTrue(is_noise_line("Rakib Hasan, www.facebook.com/rakibhasan.jnu"))
        self.assertTrue(is_noise_line("email:rakibhasan2127@gmail.com"))
        self.assertTrue(is_noise_line("25/25"))
        self.assertTrue(is_noise_line("23/25"))
        self.assertTrue(is_noise_line("SI No."))
        self.assertTrue(is_noise_line("Blood Group Area Name"))
        self.assertTrue(is_noise_line("ΣΣ"))

    def test_real_content_is_not_noise(self):
        self.assertFalse(is_noise_line("Badda"))
        self.assertFalse(is_noise_line("Hridoy Islam Hridu"))
        self.assertFalse(is_noise_line("01710-027589"))


class ParseFreeformTest(unittest.TestCase):
    def test_typical_roster_rows(self):
        text = (
            "962\n0+\nZirabo\nMr. Ashik\n01710-027589\n"
            "963\nO-\nBadda\nHridoy Islam Hridu\n01683-626187\n"
        )
        records = parse_freeform(text)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["si_no"], 962)
        self.assertEqual(records[0]["blood_group"], "O+")
        self.assertEqual(records[0]["area"], "Zirabo")
        self.assertEqual(records[0]["name"], "Mr. Ashik")
        self.assertEqual(records[0]["phones"], ["01710027589"])
        self.assertEqual(records[1]["si_no"], 963)
        self.assertEqual(records[1]["name"], "Hridoy Islam Hridu")

    def test_page_marker_and_stray_digits(self):
        text = (
            "964\n25/25\n966\nO-\nBadda\nMd. Noruddin\n01954-392545\n2\n"
        )
        records = parse_freeform(text)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "Md. Noruddin")
        self.assertIn("01954392545", records[0]["phones"])

    def test_missing_name_record_flagged(self):
        text = "O-\nBashabo\n01521-402247\n"
        records = parse_freeform(text)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["name"])
        self.assertTrue(any("name" in e for e in record_errors(records[0])))

    def test_inline_page_marker_with_group(self):
        text = "23/25 0+\nPolashpur\nMd. Sumon Mallik\n01670-682841\n01733-190484\n"
        records = parse_freeform(text)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["blood_group"], "O+")
        self.assertEqual(records[0]["area"], "Polashpur")
        self.assertEqual(records[0]["phones"],
                         ["01670682841", "01733190484"])

    def test_group_area_name_single_line(self):
        text = "O+ Mirpur Hasan Rony 01634-337351\n"
        records = parse_freeform(text)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["blood_group"], "O+")
        self.assertEqual(records[0]["area"], "Mirpur")
        self.assertEqual(records[0]["name"], "Hasan Rony")

    def test_multiline_name_merged(self):
        text = "889\n0+\nPuran Dhaka\nRakib Ul Atid\nAsad\n01983-591819\n"
        records = parse_freeform(text)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["area"], "Puran Dhaka")
        self.assertEqual(records[0]["name"], "Rakib Ul Atid Asad")

    def test_footer_and_merged_names(self):
        text = (
            "O+\nMirpur\nArafat Ahmed\nArif Hossan\n01716-667441\n"
            "Rakib Hasan, www.facebook.com/rakibhasan.jnu\n"
        )
        records = parse_freeform(text)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "Arafat Ahmed Arif Hossan")

    def test_duplicate_phones_deduped(self):
        text = "O+\nMirpur\nMehedi\n01626-580798\n01626-580798\n"
        records = parse_freeform(text)
        self.assertEqual(records[0]["phones"], ["01626580798"])


class ParseCsvTest(unittest.TestCase):
    def test_full_row(self):
        records = parse_csv("962,O+,Zirabo,Mr. Ashik,01710-027589\n")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["si_no"], 962)
        self.assertEqual(records[0]["blood_group"], "O+")
        self.assertEqual(records[0]["area"], "Zirabo")
        self.assertEqual(records[0]["name"], "Mr. Ashik")
        self.assertEqual(records[0]["phones"], ["01710027589"])

    def test_no_si_and_extra_phone(self):
        records = parse_csv(
            "O+,Mirpur,Hasan Rony,01634-337351,01911-230000\n")
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["si_no"])
        self.assertEqual(records[0]["phones"],
                         ["01634337351", "01911230000"])

    def test_garbage_rows_skipped(self):
        records = parse_csv("not a donor row\n\nO+,Badda,Md. Noruddin,01954392545\n")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "Md. Noruddin")


class AutoDetectTest(unittest.TestCase):
    def test_csv_detected(self):
        text = "962,O+,Zirabo,Mr. Ashik,01710-027589\n963,O-,Badda,Hridoy,01683-626187\n"
        records = parse_donor_text(text, mode="auto")
        self.assertEqual(len(records), 2)

    def test_freeform_detected(self):
        text = "962\n0+\nZirabo\nMr. Ashik\n01710-027589\n"
        records = parse_donor_text(text, mode="auto")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "Mr. Ashik")

    def test_empty_text(self):
        self.assertEqual(parse_donor_text(""), [])
        self.assertEqual(parse_donor_text(None), [])


class ValidationAndEmailTest(unittest.TestCase):
    def test_valid_record_no_errors(self):
        rec = {"si_no": 962, "blood_group": "O+", "area": "Zirabo",
               "name": "Mr. Ashik", "phones": ["01710027589"]}
        self.assertEqual(record_errors(rec), [])
        self.assertEqual(record_email(rec), "donor.01710027589@import.lifelink")
        self.assertEqual(record_primary_phone(rec), "01710027589")

    def test_missing_fields_flagged(self):
        rec = {"si_no": None, "blood_group": None, "area": None,
               "name": None, "phones": []}
        errors = record_errors(rec)
        self.assertEqual(len(errors), 3)

    def test_email_fallback_without_phone(self):
        rec = {"si_no": None, "blood_group": "O+", "area": None,
               "name": "Hasan Rony", "phones": []}
        self.assertTrue(record_email(rec).endswith("@import.lifelink"))

    def test_import_password_meets_policy(self):
        self.assertGreaterEqual(len(DEFAULT_IMPORT_PASSWORD), 8)
        self.assertTrue(any(c.isupper() for c in DEFAULT_IMPORT_PASSWORD))
        self.assertTrue(any(c.islower() for c in DEFAULT_IMPORT_PASSWORD))
        self.assertTrue(any(c.isdigit() for c in DEFAULT_IMPORT_PASSWORD))
        self.assertTrue(any(c in "!@#$%^&*(),.?\":{}|<>" for c in DEFAULT_IMPORT_PASSWORD))


if __name__ == "__main__":
    unittest.main(verbosity=2)
