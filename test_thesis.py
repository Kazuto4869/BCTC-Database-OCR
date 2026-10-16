import contextlib
import csv
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import weakref
from unittest.mock import patch

import thesis


class LocalOCRTests(unittest.TestCase):
    def test_direct_gpu_backend_does_not_contact_server(self):
        args = SimpleNamespace(ocr_backend="direct", model_path="weights/DotsMOCR", ocr_max_tokens=16000)
        with patch.object(thesis, "check_ocr_server", side_effect=AssertionError("must not contact server")):
            backends, _ = thesis.build_backends(args)
        self.assertEqual(backends[0]["kind"], "direct")

    def test_direct_gpu_response_is_validated_without_http(self):
        backend = {"kind": "direct", "model_path": "weights/DotsMOCR", "max_tokens": 16000, "prompt": thesis.OCR_PROMPT}
        with patch("dots_mocr_gpu.recognize", return_value='[{"category":"Text","text":"loan"}]'), \
                patch.object(thesis.requests, "post", side_effect=AssertionError("no HTTP")):
            self.assertEqual(list(thesis.ocr_response_to_page(thesis._call_backend(b"PNG", backend))), ["loan"])
        with patch("dots_mocr_gpu.recognize", return_value='broken'):
            with self.assertRaises(thesis.OCRContentError):
                thesis._call_backend(b"PNG", backend)

    def test_direct_gpu_memory_failure_retries_smaller_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ABC_2021.pdf"
            with thesis.fitz.open() as doc:
                doc.new_page()
                doc.save(path)
            backend = {"kind": "direct", "name": "GPU", "model_path": tmp, "prompt": thesis.OCR_PROMPT, "max_tokens": 16000, "retries": 1}
            seen = []
            allocations = []
            class Allocation:
                pass
            def recognize(png, be):
                seen.append(be.get("max_pixels", 786432))
                if len(seen) == 1:
                    allocation = Allocation()
                    allocations.append(weakref.ref(allocation))
                    raise ValueError("GPU thiếu VRAM")
                self.assertIsNone(allocations[0](), "Failed generation must be released before retry")
                return '[{"category":"Text","text":"recovered"}]'
            with patch("dots_mocr_gpu.recognize", side_effect=recognize):
                result = thesis.ocr_one_page(str(path), 0, {"rotate": "off", "backends": [backend]})
            self.assertEqual(list(thesis.ocr_response_to_page(result["raw"])), ["recovered"])
            self.assertEqual(seen, [786432, 524288])
    def test_truncated_page_retries_with_different_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ABC_2021.pdf"
            with thesis.fitz.open() as doc:
                doc.new_page()
                doc.save(path)
            responses = [
                SimpleNamespace(status_code=200, json=lambda: {"choices": [{"finish_reason": "length", "message": {"content": '[]'}}]}),
                SimpleNamespace(status_code=200, json=lambda: {"choices": [{"finish_reason": "stop", "message": {"content": '[{"category":"Text","text":"recovered"}]'}}]}),
            ]
            cfg = {"dpi": 200, "rotate": "off", "backends": [{"name": "dots.mocr", "url": "http://localhost:8000/v1", "prompt": thesis.OCR_PROMPT, "retries": 3}]}
            with patch.object(thesis.requests, "post", side_effect=responses) as post, patch.object(thesis.time, "sleep"):
                result = thesis.ocr_one_page(str(path), 0, cfg)
            self.assertEqual(list(thesis.ocr_response_to_page(result["raw"])), ["recovered"])
            images = [call.kwargs["json"]["messages"][0]["content"][0]["image_url"]["url"] for call in post.call_args_list]
            self.assertNotEqual(images[0], images[1])

    def test_malformed_api_layout_is_rejected(self):
        response = SimpleNamespace(status_code=200, json=lambda: {
            "choices": [{"finish_reason": "stop", "message": {"content": '[{"text":"broken'}}]})
        backend = {"url": "http://localhost:8000/v1", "prompt": thesis.OCR_PROMPT}
        with patch.object(thesis.requests, "post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "JSON"):
                thesis._call_openai_compat(b"PNG", backend)

    def test_truncated_api_completion_is_rejected(self):
        response = SimpleNamespace(status_code=200, json=lambda: {
            "choices": [{"finish_reason": "length", "message": {"content": '[]'}}]})
        backend = {"url": "http://localhost:8000/v1", "prompt": thesis.OCR_PROMPT}
        with patch.object(thesis.requests, "post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "token"):
                thesis._call_openai_compat(b"PNG", backend)

    def test_saved_pdf_is_skipped_unless_rerun_is_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Source"
            source.mkdir()
            (source / "ABC_2021.pdf").touch()
            output = Path(tmp) / "result"
            argv = ["thesis.py", "--pdf-dir", str(source), "--no-ocr", "--workers", "1", "--output-dir", str(output)]
            first = {"loans": [], "log": {"File_Name": "ABC_2021.pdf", "Status_Flag": "[OK]", "Text_Source": "OCR"}}
            with contextlib.redirect_stdout(io.StringIO()):
                with patch("sys.argv", argv), patch.object(thesis, "process_file", return_value=first):
                    thesis.main()
                with patch("sys.argv", [part for part in argv if part != "--no-ocr"]), \
                        patch.object(thesis, "check_ocr_server", side_effect=AssertionError("server not needed")), \
                        patch.object(thesis, "process_file", side_effect=AssertionError("must skip")):
                    thesis.main()
                updated = {"loans": [{"Corporate_Ticker": "ABC", "Year": 2021, "Loan_Amount_VND": 456}], "log": first["log"]}
                with patch("sys.argv", argv + ["--rerun"]), patch.object(thesis, "process_file", return_value=updated):
                    thesis.main()
            with (output / "Corporate_Bank_Loans.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([r["Loan_Amount_VND"] for r in rows], ["456"])

    def test_completed_file_is_saved_before_next_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Source"
            source.mkdir()
            for name in ("ABC_2021.pdf", "ABC_2022.pdf"):
                (source / name).touch()
            output = Path(tmp) / "result"
            first = {"loans": [], "log": {"File_Name": "ABC_2021.pdf", "Status_Flag": thesis.CHECK,
                                          "Text_Source": "OCR", "Notes": "test completed"}}
            argv = ["thesis.py", "--pdf-dir", str(source), "--no-ocr", "--workers", "1",
                    "--debug-dir", "", "--output", str(output)]
            with patch("sys.argv", argv), \
                    patch.object(thesis, "process_file", side_effect=[first, RuntimeError("second file failed")]), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "second file failed"):
                    thesis.main()
            with (output / "Extraction_Logs.csv").open(encoding="utf-8-sig", newline="") as stream:
                logs = list(csv.DictReader(stream))
            self.assertEqual([row["File_Name"] for row in logs], ["ABC_2021.pdf"])

    def test_server_error_keeps_underlying_connection_reason(self):
        args = SimpleNamespace(ocr_url="http://localhost:8000/v1")
        error = "HTTPConnectionPool: " + "x" * 100 + " Connection refused"
        with patch.object(thesis, "check_ocr_server", return_value=(False, error)):
            backends, messages = thesis.build_backends(args)
        self.assertEqual(backends, [])
        self.assertIn(error, messages[0])

    def test_gemini_key_never_enables_remote_fallback(self):
        args = SimpleNamespace(
            ocr_url="http://localhost:8000/v1", ocr_model="model",
            ocr_no_img_prefix=False, ocr_max_tokens=16000,
        )
        with patch.dict(os.environ, {"GEMINI_API_KEY": "unused-test-key"}), \
                patch.object(thesis, "check_ocr_server", return_value=(True, "")):
            backends, _ = thesis.build_backends(args)
        self.assertEqual([be["name"] for be in backends], ["dots.mocr"])

    def test_missing_server_reports_error_without_recording_pdf_as_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Source"
            source.mkdir()
            (source / "ABC_2021.pdf").write_bytes(b"not a real PDF")
            output = Path(tmp) / "result"
            argv = ["thesis.py", "--ocr-backend", "vllm", "--pdf-dir", str(source), "--output", str(output),
                    "--ocr-cache", str(Path(tmp) / "cache"), "--workers", "1"]
            with patch("sys.argv", argv), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(thesis, "check_ocr_server", return_value=(False, "offline")), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SystemExit, "dots.mocr"):
                    thesis.main()
            with (output / "Extraction_Logs.csv").open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])

    def test_ocr_probe_exits_with_error_when_model_request_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ABC_2021.pdf"
            with thesis.fitz.open() as doc:
                doc.new_page()
                doc.save(path)
            backend = {"name": "dots.mocr", "kind": "openai"}
            with patch.object(thesis, "build_backends", return_value=([backend], [])), \
                    patch.object(thesis, "_call_openai_compat", side_effect=RuntimeError("model failed")), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SystemExit, "dots.mocr"):
                    thesis.run_test_ocr(SimpleNamespace(), [str(path)])

    def test_local_request_preserves_image_prefix_and_html_table(self):
        raw = '[{"category":"Table","text":"<table><tr><td>VCB</td><td>1.234.567</td></tr></table>"}]'
        response = SimpleNamespace(status_code=200, json=lambda: {
            "choices": [{"message": {"content": raw}}]})
        backend = {"url": "http://localhost:8000/v1", "model": "model",
                   "prefix": True, "prompt": thesis.OCR_PROMPT}
        with patch.object(thesis.requests, "post", return_value=response) as post:
            page = thesis.ocr_response_to_page(thesis._call_openai_compat(b"PNG", backend))
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "model")
        content = payload["messages"][0]["content"]
        self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertTrue(content[1]["text"].startswith(thesis.IMG_PREFIX))
        self.assertEqual(page.blocks[0][0], "table")
        self.assertEqual(page.blocks[0][1], [["VCB", "1.234.567"]])


if __name__ == "__main__":
    unittest.main()
