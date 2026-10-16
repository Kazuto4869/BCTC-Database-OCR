import unittest
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import thesis


class StatementTests(unittest.TestCase):
    def test_auto_mode_reuses_good_ocr_cache_and_skips_native_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ABC_2021.pdf"
            path.write_bytes(b"source")
            cfg = {"cache_dir": str(Path(tmp) / "cache"), "threads": 1}
            cached = {"raw": '[{"category":"Text","text":"cached OCR"}]', "rot": 0, "backend": "dots.mocr"}
            with patch.object(thesis, "ocr_one_page", return_value=cached):
                thesis.ocr_pages(str(path), [0], cfg)
            native = [["native text " * 30], ["another native page " * 30]]
            with patch.object(thesis, "ocr_one_page", side_effect=AssertionError("native page needs no OCR")):
                pages, failed, meta = thesis.ocr_pages(str(path), [0, 1], cfg, native=native)
            self.assertEqual(list(pages[0]), ["cached OCR"])
            self.assertEqual(pages[1], native[1])
            self.assertEqual(meta["native"], [1])
            self.assertEqual(failed, [])
    def test_statement_currency_header_overrides_intro_summary_unit(self):
        pages = [["Kết quả kinh doanh", "Đơn vị tính: Tỷ đồng"],
                 ["Bảng cân đối kế toán", "Mã số Thuyết minh 31/12/2021 VND 1/1/2021 VND"]]
        self.assertEqual(thesis.detect_unit(pages, [1]), (1, "Đồng"))

    def test_explicit_scaled_unit_overrides_plain_currency_header(self):
        pages = [["Bảng cân đối kế toán", "Đơn vị tính: Triệu đồng", "31/12/2021 VND 1/1/2021 VND"]]
        self.assertEqual(thesis.detect_unit(pages, [0]), (1000000, "Triệu đồng"))
    def test_other_banks_do_not_inherit_previous_named_bank(self):
        state = thesis.SegState(2021, 1, "26. Vay dài hạn")
        state.line("Ngân hàng TMCP Kỹ Thương Việt Nam", 45)
        state.line("Các ngân hàng khác", 45)
        state.line("Các khoản vay khác 14.885.123.598 Ngày 31 tháng 12 năm 2027", 45)
        self.assertEqual(state.rows[0]["Bank_Ticker_Matched"], "UNALLOCATED")
    def test_current_portion_subtotal_does_not_change_next_bank_section(self):
        state = thesis.SegState(2022, 1, "26.3 Các khoản vay từ ngân hàng dài hạn")
        state.line("Vay dài hạn đến hạn trả 100.000 90.000", 46)
        state.line("Ngân hàng TMCP Á Châu", 46)
        state.line("Khoản vay 1 1.000.000 Ngày 31 tháng 12 năm 2025", 46)
        self.assertEqual(state.rows[0]["Loan_Type"], thesis.LT_LOAN)
        self.assertNotIn("CURRENT_PORTION_LT", state.rows[0]["Note_Flag"])
    def test_bank_summary_and_facility_details_are_not_counted_twice(self):
        rows = [{"Loan_Type": thesis.ST_LOAN, "Bank_Name_Raw": name, "Bank_Ticker_Matched": "ACB",
                 "amount_raw": amount, "ccy": "VND", "fx_pending": False, "Is_Direct_Loan": True,
                 "Source_Page": 1, "Note_Flag": flag}
                for name, amount, flag in [("ACB", 100, "BANK_SUMMARY"), ("ACB Khoản vay 1", 40, "BANK_DETAIL"), ("ACB Khoản vay 2", 60, "BANK_DETAIL")]]
        with patch.object(thesis, "find_loan_segments", return_value=[[]]), \
                patch.object(thesis, "process_segment", return_value=(rows, {}, {})), \
                patch.object(thesis, "narrative_rows", return_value=[]):
            result = thesis.analyze([["text"]], 2021)
        self.assertEqual(sum(r["Loan_Amount_VND"] for r in result["rows"]), 100)

    def test_table_loan_rows_keep_the_bank_heading(self):
        state = thesis.SegState(2021, 1, "26. Vay ngắn hạn")
        state.line("Ngân hàng TMCP Á Châu", 1)
        grid = [["", "Số cuối năm (VND)", "Kỳ hạn trả gốc"], ["Khoản vay 1", "1.000.000", "Ngày 31 tháng 12 năm 2022"], ["Khoản vay 2", "2.000.000", "Ngày 31 tháng 12 năm 2022"]]
        state.table(grid, [[False] * 3 for _ in grid], 1)
        self.assertEqual([r["Bank_Ticker_Matched"] for r in state.rows], ["ACB", "ACB"])
    def test_standalone_maturity_totals_are_not_lender_rows(self):
        state = thesis.SegState(2021, 1, "26. VAY VÀ NỢ")
        state.line("Ngắn hạn 2.962.805.881.517 2.068.948.248.001", 41)
        state.line("Nợ dài hạn đến hạn trả 1.809.331.663.162 1.046.830.795.613", 41)
        state.line("Dài hạn 1.517.104.929.560 3.876.368.180.503", 41)
        state.line("Ngân hàng TMCP Á Châu 1.000.000 900.000", 42)
        self.assertEqual(len(state.rows), 1)
        self.assertEqual(state.rows[0]["Loan_Type"], thesis.LT_LOAN)
        self.assertNotIn("CURRENT_PORTION_LT", state.rows[0]["Note_Flag"])

    def test_inline_loan_uses_balance_not_collateral_amount(self):
        state = thesis.SegState(2021, 1, "26. Vay ngắn hạn")
        state.line("Ngân hàng TMCP Việt Nam Thịnh Vượng", 42)
        state.line("Khoản vay 1 259.309.629.768 Từ ngày 17 tháng 1 năm 2022 Hợp đồng tiền gửi trị giá 45.017.409.953 VND", 42)
        state.line("Khoản vay 2 94.401.441.490 Hợp đồng tiền gửi trị giá 10.000.000.000 VND", 42)
        self.assertEqual([r["amount_raw"] for r in state.rows], [259309629768, 94401441490])
        self.assertEqual([r["Bank_Ticker_Matched"] for r in state.rows], ["VPB", "VPB"])
    def test_failed_page_does_not_abort_later_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ABC_2021.pdf"
            path.write_bytes(b"source")
            def response(path, index, cfg):
                if index == 0:
                    raise RuntimeError("bad page")
                return {"raw": '[{"category":"Text","text":"saved"}]', "rot": 0, "backend": "dots.mocr"}
            with patch.object(thesis, "ocr_one_page", side_effect=response):
                pages, failed, meta = thesis.ocr_pages(str(path), range(7), {"cache_dir": tmp, "threads": 1})
            self.assertEqual(set(pages), set(range(1, 7)))
            self.assertEqual(failed, [0])
            self.assertEqual(meta["errors"]["bad page"], 1)

    def test_balance_code_before_description_is_supported(self):
        lines = ["320 8. Vay ngắn hạn 26 2.962.805.881.517 2.068.948.248.001",
                 "338 2. Vay dài hạn 26 1.517.104.929.560 3.876.368.180.503"]
        self.assertEqual(thesis.count_code_lines(lines), 2)
        tail = thesis.parse_code_value(lines[0], "320")
        self.assertIsNotNone(tail)
        self.assertEqual(thesis.pick_statement_value(tail), 2962805881517)
        self.assertEqual(thesis.count_code_lines(["320 Vay ngắn hạn 1.000.000 900.000"]), 1)
        self.assertEqual(thesis.count_code_lines(["320 8. Vay ngắn hạn 1.000.000 900.000"]), 1)

    def test_log_sum_matches_exported_rows(self):
        rows = [{"Loan_Amount_VND": 100, "Bank_Ticker_Matched": "ACB", "Note_Flag": flag}
                for flag in ("", "CURRENT_PORTION_LT")]
        result = {"fin": {"320": 100, "338": 0}, "rows": rows, "found": True,
                  "unit_label": "Đồng", "notes": [], "stats": {}, "fx": {}}
        info = {"ocr_pages": 0, "ocr_failed": 0, "rotated": 0}
        with patch.object(thesis, "load_pages", return_value=([["text"]], "fitz", info)), \
                patch.object(thesis, "analyze", return_value=result):
            extracted = thesis.process_file("ABC_2021.pdf")
        self.assertEqual(extracted["log"]["Sum_Extracted_Loans"], sum(r["Loan_Amount_VND"] for r in extracted["loans"]))
        self.assertEqual(extracted["log"]["Status_Flag"], thesis.MISMATCH)

    def test_missing_long_term_debt_is_not_zero(self):
        result = {"fin": {"320": 100}, "rows": [], "found": True,
                  "unit_label": "Đồng", "notes": [], "stats": {}, "fx": {}}
        with patch.object(thesis, "load_pages", return_value=([["text"]], "fitz", {"ocr_pages": 0, "ocr_failed": 0, "rotated": 0})), \
                patch.object(thesis, "analyze", return_value=result):
            extracted = thesis.process_file("ABC_2021.pdf")
        self.assertIsNone(extracted["log"]["Total_Debt_BS"])

    def test_equal_original_amounts_in_different_currencies_are_kept(self):
        rows = [{"Loan_Type": thesis.ST_LOAN, "Bank_Name_Raw": "ACB", "Bank_Ticker_Matched": "ACB",
                 "amount_raw": 100, "ccy": ccy, "fx_pending": True, "Is_Direct_Loan": True,
                 "Source_Page": 1, "Note_Flag": ""} for ccy in ("USD", "EUR")]
        with patch.object(thesis, "find_loan_segments", return_value=[[]]), \
                patch.object(thesis, "process_segment", return_value=(rows, {}, {})), \
                patch.object(thesis, "narrative_rows", return_value=[]):
            result = thesis.analyze([["text"]], 2021)
        self.assertEqual([r["Currency"] for r in result["rows"]], ["USD", "EUR"])

    def test_invalid_cached_layout_is_retried_when_backend_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ABC_2021.pdf"
            path.write_bytes(b"source")
            cfg = {"cache_dir": str(Path(tmp) / "cache"), "threads": 1}
            bad = {"raw": '[{"category":"Text","text":"broken', "rot": 0, "backend": "dots.mocr"}
            with patch.object(thesis, "ocr_one_page", return_value=bad):
                thesis.ocr_pages(str(path), [0], cfg)
            good = {"raw": '[{"category":"Text","text":"corrected"}]', "rot": 0, "backend": "dots.mocr"}
            with patch.object(thesis, "ocr_one_page", return_value=good):
                pages, failed, meta = thesis.ocr_pages(str(path), [0], dict(cfg, backends=[{"name": "dots.mocr"}]))
            self.assertEqual(list(pages[0]), ["corrected"])
            self.assertEqual(meta["invalid_json"], [])
            self.assertEqual(failed, [])

    def test_explanatory_footnote_is_not_used_as_lender_for_stray_number(self):
        state = thesis.SegState(2021, 1, "24b. Vay dài hạn")
        state.line("(vi) Khoản vay Ngân hàng TMCP Tiên Phong để đầu tư máy móc, được đảm bảo bằng tài sản thế chấp.", 56)
        state.line("498", 56)
        self.assertEqual(state.rows, [])

    def test_changed_pdf_does_not_reuse_old_page_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ABC_2021.pdf"
            path.write_bytes(b"old source")
            cfg = {"cache_dir": str(Path(tmp) / "cache"), "threads": 1}
            def response(text):
                return {"raw": '[{"category":"Text","text":"' + text + '"}]', "rot": 0, "backend": "dots.mocr"}
            with patch.object(thesis, "ocr_one_page", return_value=response("old result")):
                thesis.ocr_pages(str(path), [0], cfg)
            timestamp = path.stat().st_mtime_ns
            path.write_bytes(b"new source")
            os.utime(path, ns=(timestamp, timestamp))
            with patch.object(thesis, "ocr_one_page", return_value=response("new result")):
                pages, failed, _ = thesis.ocr_pages(str(path), [0], cfg)
            self.assertEqual(list(pages[0]), ["new result"])
            self.assertEqual(failed, [])

    def test_short_numbered_loan_headings_are_detected(self):
        pages = [["23. Vay", "23a. Vay ngắn hạn", "Số cuối năm Số đầu năm",
                  "Ngân hàng TMCP Á Châu 1.000.000 900.000",
                  "23b. Vay dài hạn", "Số cuối năm Số đầu năm",
                  "Ngân hàng TMCP Á Châu 2.000.000 1.900.000", "24. Thuế phải trả"]]
        segments = thesis.find_loan_segments(pages, 0)
        rows = [row for segment in segments for row in thesis.process_segment(segment, 2024, 1)[0]]
        self.assertEqual([(r["Loan_Type"], r["amount_raw"]) for r in rows],
                         [(thesis.ST_LOAN, 1000000), (thesis.LT_LOAN, 2000000)])

    def test_balance_code_accepts_multiple_note_references(self):
        tail = thesis.parse_code_value("Vay ngắn hạn 320 V.23a,c 8.444.231.302.085 9.385.205.395.358", "320")
        self.assertIsNotNone(tail)
        self.assertEqual(thesis.pick_statement_value(tail), 8444231302085)

    def test_loan_subtotals_with_note_references_are_not_detail_rows(self):
        state = thesis.SegState(2021, 1, "24a. Vay ngắn hạn")
        state.line("Vay dài hạn đến hạn trả (xem thuyết minh số V.24b) 480.010.499.140 614.510.863.993", 50)
        state.line("Ngân hàng TMCP Á Châu 100.000.000 90.000.000", 50)
        self.assertEqual(len(state.rows), 1)
        self.assertIn("CURRENT_PORTION_LT", state.rows[0]["Note_Flag"])
        state.line("Trái phiếu thường dài hạn 10.397.615.985.937 5.787.657.889.143", 52)
        self.assertEqual(len(state.rows), 1)
        self.assertEqual(state.cur_type, thesis.BOND)

    def test_restatement_table_is_not_selected_as_balance_sheet(self):
        codes = [f"Tài sản {i} 1.000.000 900.000" for i in range(100, 106)]
        adjustment = ["Bảng cân đối kế toán tổng hợp",
                      "Mã số Số liệu trước điều chỉnh Các điều chỉnh Số liệu sau điều chỉnh", *codes]
        actual = ["BẢNG CÂN ĐỐI KẾ TOÁN HỢP NHẤT", "Tại ngày 31 tháng 12 năm 2021", *codes]
        self.assertEqual(thesis.find_stmt_start([adjustment, actual], "bang can doi ke toan"), 1)

    def test_loan_codes_accept_letter_suffix_in_note_reference(self):
        line = "10. Vay và nợ thuê tài chính ngắn hạn 320 V.24a 4.248.089.036.214 6.137.586.557.020"
        tail = thesis.parse_code_value(line, "320")
        self.assertEqual(tail, ["V.24a", "4.248.089.036.214", "6.137.586.557.020"])
        self.assertEqual(thesis.pick_statement_value(tail), 4248089036214)

    def test_balance_sheet_continues_through_four_pages(self):
        pages = [
            ["BẢNG CÂN ĐỐI KẾ TOÁN HỢP NHẤT", "Tại ngày 31 tháng 12 năm 2021", "Đơn vị tính: VND",
             *[f"Tài sản {i} 1.000.000 900.000" for i in range(100, 106)]],
            ["Bảng cân đối kế toán hợp nhất (tiếp theo)", "TỔNG CỘNG TÀI SẢN 270 48.952.440.778.613 49.116.564.806.591"],
            ["Bảng cân đối kế toán hợp nhất (tiếp theo)", "NỢ PHẢI TRẢ 300 31.807.520.030.579 32.538.992.444.118"],
            ["Bảng cân đối kế toán hợp nhất (tiếp theo)", "VỐN CHỦ SỞ HỮU 400 17.144.920.748.034 16.577.572.362.473"],
            ["BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH", *[f"Doanh thu {i} 1.000.000 900.000" for i in range(20, 26)],
             "Lợi nhuận 50 1.000.000 900.000"],
        ]
        self.assertEqual(thesis.analyze(pages, 2021)["fin"].get("400"), 17144920748034)


if __name__ == "__main__":
    unittest.main()
