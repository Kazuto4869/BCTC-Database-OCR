"""Persist one PDF at a time in SQLite; stream CSV snapshots from disk."""

import csv
import os
from pathlib import Path
import sqlite3
import tempfile


LOAN_COLUMNS = ["Corporate_Ticker", "Year", "Loan_Type", "Bank_Name_Raw", "Bank_Ticker_Matched",
                "Loan_Amount_VND", "Is_Direct_Loan", "Currency", "Original_Amount", "FX_Rate", "Source_Page", "Note_Flag"]
LOG_COLUMNS = ["File_Name", "Status_Flag", "Sum_Extracted_Loans", "Total_Debt_BS", "Text_Source", "Notes"]
_TYPES = {"Year": "INTEGER", "Loan_Amount_VND": "INTEGER", "Is_Direct_Loan": "INTEGER",
          "Original_Amount": "REAL", "FX_Rate": "REAL", "Source_Page": "INTEGER",
          "Sum_Extracted_Loans": "INTEGER", "Total_Debt_BS": "INTEGER"}


class ResultStore:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.output_dir / "extraction.sqlite3")
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA synchronous = FULL")
            self.connection.execute("PRAGMA cache_size = -4096")
            with self.connection:
                log_schema = ", ".join(f"{c} {_TYPES.get(c, 'TEXT')}" for c in LOG_COLUMNS)
                loan_schema = ", ".join(f"{c} {_TYPES.get(c, 'TEXT')}" for c in LOAN_COLUMNS)
                self.connection.execute(f"CREATE TABLE IF NOT EXISTS logs (file_key TEXT PRIMARY KEY, {log_schema})")
                self.connection.execute(f"CREATE TABLE IF NOT EXISTS loans (file_key TEXT NOT NULL REFERENCES logs(file_key) ON DELETE CASCADE, {loan_schema})")
                self.connection.execute("CREATE INDEX IF NOT EXISTS loans_file_key ON loans(file_key)")
            self.export_csv()
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def save_file(self, path, result):
        key = str(Path(path).resolve())
        log = result["log"]
        invalid = []
        for column in ("Sum_Extracted_Loans", "Total_Debt_BS"):
            value = log.get(column)
            if isinstance(value, int) and not -(2**63) <= value < 2**63:
                invalid.append(f"{column}={value}")
                log[column] = None
        for row in result["loans"]:
            value = row.get("Loan_Amount_VND")
            if isinstance(value, int) and not -(2**63) <= value < 2**63:
                invalid.append(f"trang {row.get('Source_Page')}: Loan_Amount_VND={value}")
                row["Loan_Amount_VND"] = None
                row["Note_Flag"] = ";".join(filter(None, [row.get("Note_Flag"), "AMOUNT_OUT_OF_RANGE"]))
                log["Sum_Extracted_Loans"] = None
        if invalid:
            log["Status_Flag"] = "[CHECK_MANUAL]"
            log["Notes"] = "; ".join(filter(None, [log.get("Notes"),
                "Số tiền vượt giới hạn SQLite; không dùng để tính tổng, cần kiểm tra đơn vị/OCR: " + " | ".join(invalid)]))
        with self.connection:
            self.connection.execute("DELETE FROM logs WHERE file_key = ?", (key,))
            self.connection.execute(
                f"INSERT INTO logs (file_key, {', '.join(LOG_COLUMNS)}) VALUES ({', '.join('?' for _ in range(len(LOG_COLUMNS) + 1))})",
                (key, *(log.get(c) for c in LOG_COLUMNS)))
            self.connection.executemany(
                f"INSERT INTO loans (file_key, {', '.join(LOAN_COLUMNS)}) VALUES ({', '.join('?' for _ in range(len(LOAN_COLUMNS) + 1))})",
                ((key, *(row.get(c) for c in LOAN_COLUMNS)) for row in result["loans"]))
        self.export_csv()

    def export_csv(self):
        for table, columns, name in (("loans", LOAN_COLUMNS, "Corporate_Bank_Loans.csv"),
                                     ("logs", LOG_COLUMNS, "Extraction_Logs.csv")):
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                                 dir=self.output_dir, prefix="." + name,
                                                 suffix=".tmp", delete=False) as stream:
                    temporary_path = Path(stream.name)
                    writer = csv.writer(stream)
                    writer.writerow(columns)
                    cursor = self.connection.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY file_key, rowid")
                    writer.writerows(cursor)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, self.output_dir / name)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

    def summary(self):
        return dict(self.connection.execute("SELECT Status_Flag, COUNT(*) FROM logs GROUP BY Status_Flag"))

    def has_file(self, path):
        # shortcut: identity is the PDF path; use --rerun after replacing its contents.
        key = str(Path(path).resolve())
        row = self.connection.execute("SELECT Status_Flag, Notes FROM logs WHERE file_key = ?", (key,)).fetchone()
        if row is None or row[0] == "[SCANNED_PDF]":
            return False
        notes = row[1] or ""
        return not notes.startswith(("Lỗi đọc PDF:", "Lỗi không xác định:")) and \
            "OCR lỗi " not in notes and "Cache OCR JSON không hợp lệ" not in notes

    def file_names(self):
        for (name,) in self.connection.execute("SELECT File_Name FROM logs ORDER BY file_key"):
            yield name
