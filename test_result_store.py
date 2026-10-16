import csv
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


class ResultStoreTests(unittest.TestCase):
    def test_overflow_amount_is_flagged_and_next_pdf_can_be_saved(self):
        module = importlib.import_module("result_store")
        with tempfile.TemporaryDirectory() as tmp:
            too_large = 57212575081056 * 1000000000
            result = {"loans": [{"Loan_Amount_VND": too_large, "Source_Page": 59}],
                      "log": {"File_Name": "HPG_2021.pdf", "Status_Flag": "[OK]",
                              "Sum_Extracted_Loans": too_large, "Total_Debt_BS": too_large}}
            with module.ResultStore(tmp) as store:
                store.save_file(str(Path(tmp) / "HPG_2021.pdf"), result)
                store.save_file(str(Path(tmp) / "HPG_2022.pdf"), {"loans": [{"Loan_Amount_VND": 123}], "log": {"File_Name": "HPG_2022.pdf", "Status_Flag": "[OK]"}})
            logs = read_csv(Path(tmp) / "Extraction_Logs.csv")
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0]["Status_Flag"], "[CHECK_MANUAL]")
            self.assertEqual(logs[0]["Sum_Extracted_Loans"], "")
            self.assertEqual(logs[0]["Total_Debt_BS"], "")
            self.assertIn(str(too_large), logs[0]["Notes"])
            rows = read_csv(Path(tmp) / "Corporate_Bank_Loans.csv")
            self.assertEqual(rows[0]["Loan_Amount_VND"], "")
            self.assertIn("AMOUNT_OUT_OF_RANGE", rows[0]["Note_Flag"])
            self.assertEqual(rows[1]["Loan_Amount_VND"], "123")
    def test_invalid_cached_ocr_is_retried(self):
        module = importlib.import_module("result_store")
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ABC_2021.pdf")
            with module.ResultStore(tmp) as store:
                store.save_file(path, {"loans": [], "log": {"Status_Flag": "[CHECK_MANUAL]", "Notes": "Cache OCR JSON không hợp lệ ở trang 1"}})
                self.assertFalse(store.has_file(path))
    def test_failed_ocr_pdf_is_not_treated_as_completed(self):
        module = importlib.import_module("result_store")
        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / "BCM_2021.pdf")
            with module.ResultStore(tmp) as store:
                store.save_file(source, {"loans": [], "log": {"File_Name": "BCM_2021.pdf", "Status_Flag": "[SCANNED_PDF]"}})
                self.assertFalse(store.has_file(source))

    def test_saved_pdf_is_detected_after_reopening_database(self):
        module = importlib.import_module("result_store")
        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / "BCM_2021.pdf")
            with module.ResultStore(tmp) as store:
                self.assertFalse(store.has_file(source))
                store.save_file(source, {"loans": [], "log": {"File_Name": "BCM_2021.pdf", "Status_Flag": "[OK]"}})
            with module.ResultStore(tmp) as reopened:
                self.assertTrue(reopened.has_file(source))
                self.assertFalse(reopened.has_file(str(Path(tmp) / "BCM_2022.pdf")))

    def test_csv_headers_exist_before_first_pdf_finishes(self):
        module = importlib.import_module("result_store")
        with tempfile.TemporaryDirectory() as tmp:
            with module.ResultStore(tmp) as store:
                for name in ("Corporate_Bank_Loans.csv", "Extraction_Logs.csv"):
                    path = Path(tmp) / name
                    self.assertTrue(path.is_file())
                    self.assertEqual(read_csv(path), [])
                self.assertTrue((Path(tmp) / "extraction.sqlite3").is_file())

    def test_rerun_replaces_pdf_rows_without_duplicates(self):
        module = importlib.import_module("result_store")
        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / "BCM_2021.pdf")
            first = {"loans": [{"Corporate_Ticker": "BCM", "Year": 2021,
                                "Bank_Name_Raw": 'Ngân hàng, "Á Châu"\nChi nhánh',
                                "Loan_Amount_VND": 12345678901234}],
                     "log": {"File_Name": "BCM_2021.pdf", "Status_Flag": "[CHECK_MANUAL]"}}
            with module.ResultStore(tmp) as store:
                store.save_file(source, first)
                self.assertEqual(read_csv(Path(tmp) / "Corporate_Bank_Loans.csv")[0]["Bank_Name_Raw"],
                                 first["loans"][0]["Bank_Name_Raw"])
                self.assertEqual(read_csv(Path(tmp) / "Corporate_Bank_Loans.csv")[0]["Loan_Amount_VND"],
                                 "12345678901234")
                store.save_file(source, {"loans": [], "log": first["log"]})
                self.assertEqual(read_csv(Path(tmp) / "Corporate_Bank_Loans.csv"), [])
                self.assertEqual(len(read_csv(Path(tmp) / "Extraction_Logs.csv")), 1)
            with module.ResultStore(tmp) as reopened:
                self.assertEqual(reopened.summary(), {"[CHECK_MANUAL]": 1})

    def test_failed_database_update_keeps_previous_results(self):
        module = importlib.import_module("result_store")
        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / "BCM_2021.pdf")
            first = {"loans": [{"Loan_Amount_VND": 123}],
                     "log": {"File_Name": "BCM_2021.pdf", "Status_Flag": "[OK]"}}
            with module.ResultStore(tmp) as store:
                store.save_file(source, first)
                broken = {"loans": [{"Loan_Amount_VND": object()}], "log": first["log"]}
                with self.assertRaises(sqlite3.Error):
                    store.save_file(source, broken)
            self.assertEqual(read_csv(Path(tmp) / "Corporate_Bank_Loans.csv")[0]["Loan_Amount_VND"], "123")


if __name__ == "__main__":
    unittest.main()
