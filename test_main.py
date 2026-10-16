import contextlib
import csv
import importlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import thesis


class MenuTests(unittest.TestCase):
    def test_menu_can_force_ocr_every_page(self):
        main = importlib.import_module("main")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Source").mkdir()
            (root / "Source" / "ABC_2021.pdf").touch()
            with patch.object(main, "ROOT", root), patch("builtins.input", side_effect=["1", "1", "0"]), \
                    patch.object(main.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
                run.return_value.returncode = 0
                main.main(force_ocr=True)
            self.assertIn("--force-ocr", run.call_args.args[0])
    def test_menu_can_rerun_selected_files(self):
        main = importlib.import_module("main")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Source").mkdir()
            (root / "Source" / "ABC_2021.pdf").touch()
            with patch.object(main, "ROOT", root), patch("builtins.input", side_effect=["1", "2", "0"]), \
                    patch.object(main.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
                run.return_value.returncode = 0
                main.main()
            self.assertIn("--rerun", run.call_args.args[0])

    def test_parallel_files_are_saved_to_same_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Source"
            source.mkdir()
            for name in ("ABC_2021.pdf", "ABC_2022.pdf"):
                (source / name).write_bytes(b"not a real PDF")
            output = Path(tmp) / "result"
            argv = ["thesis.py", "--pdf-dir", str(source), "--no-ocr", "--workers", "2",
                    "--debug-dir", "", "--output-dir", str(output)]
            with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                thesis.main()
            with (output / "Extraction_Logs.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([r["File_Name"] for r in rows], ["ABC_2021.pdf", "ABC_2022.pdf"])

    def test_selection_supports_ranges_and_rejects_invalid_indices(self):
        main = importlib.import_module("main")
        files = [Path(f"ABC_{year}.pdf") for year in range(2021, 2026)]
        self.assertEqual(main.select_files("1,3-5,3", files), [files[i] for i in (0, 2, 3, 4)])
        for selection in ("", "0", "6", "3-1", "abc", "1,,2"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                main.select_files(selection, files)

    def test_menu_passes_exact_selected_files_and_result_paths(self):
        main = importlib.import_module("main")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Source"
            source.mkdir()
            for name in ("ABC_2021.pdf", "ABC_2022.pdf", "ABC_2023.PDF"):
                (source / name).touch()
            with patch.object(main, "ROOT", root), \
                    patch("builtins.input", side_effect=["2", "wrong", "2,3", "", "0"]), \
                    patch.object(main.subprocess, "run") as run, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                run.return_value.returncode = 0
                main.main()
            command = run.call_args.args[0]
            selected = command[command.index("--files") + 1:]
            self.assertEqual(selected, [str(source / "ABC_2022.pdf"), str(source / "ABC_2023.PDF")])
            output_dir = Path(command[command.index("--output-dir") + 1])
            self.assertEqual(output_dir.parent, root / "result")
            self.assertEqual(output_dir.name, "data")
            self.assertNotIn("--rerun", command)
            self.assertNotIn("--force-ocr", command)
            self.assertIn("0/2", output.getvalue())
            self.assertNotIn("--fallback", command)

    def test_exact_file_selection_exports_only_selected_pdf_with_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Source"
            source.mkdir()
            for name in ("ABC_2021.pdf", "ABC_2022.pdf"):
                (source / name).write_bytes(b"not a real PDF")
            selected = source / "ABC_2022.pdf"
            output = Path(tmp) / "result"
            argv = ["thesis.py", "--pdf-dir", str(source), "--no-ocr", "--workers", "1",
                    "--debug-dir", "", "--output", str(output), "--files", str(selected)]
            with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()) as console:
                thesis.main()
            with (output / "Extraction_Logs.csv").open(encoding="utf-8-sig", newline="") as stream:
                logs = list(csv.DictReader(stream))
            self.assertEqual([row["File_Name"] for row in logs], [selected.name])
            self.assertIn("[1/1]", console.getvalue())


if __name__ == "__main__":
    unittest.main()
