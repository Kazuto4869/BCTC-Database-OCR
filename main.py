#!/usr/bin/env python3
"""Menu terminal để trích xuất PDF trong Source bằng dots.mocr."""

from pathlib import Path
import argparse
import subprocess
import sys


ROOT = Path(__file__).resolve().parent


def select_files(selection, files):
    indices = set()
    for part in selection.split(","):
        bounds = part.strip().split("-")
        if len(bounds) == 1:
            first = last = int(bounds[0])
        elif len(bounds) == 2:
            first, last = map(int, bounds)
        else:
            raise ValueError("Nhập số hoặc khoảng số, ví dụ 1,3,5-8.")
        if not 1 <= first <= last <= len(files):
            raise ValueError(f"Số file phải từ 1 đến {len(files)}; khoảng số phải tăng dần.")
        indices.update(range(first - 1, last))
    return [files[i] for i in sorted(indices)]


def main(force_ocr=False):
    source = ROOT / "Source"
    while True:
        files = sorted(p for p in source.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
        print(f"\n=== TRÍCH XUẤT BCTC — dots.mocr ===\nNguồn: {source}\nCó {len(files)} file PDF.")
        print("OCR mọi trang" if force_ocr else "Đọc chữ có sẵn trong PDF; dots.mocr OCR trang scan/ít chữ.")
        print("1. Chạy tất cả file\n2. Chọn file cụ thể\n0. Thoát")
        choice = input("Chọn chức năng: ").strip()
        if choice == "0":
            return
        if choice not in ("1", "2"):
            print("Lựa chọn không hợp lệ. Nhập 1, 2 hoặc 0.")
            continue
        if not files:
            print("Không tìm thấy PDF trong Source.")
            continue
        selected = files
        if choice == "2":
            for i, path in enumerate(files, 1):
                print(f"{i:3}. {path.relative_to(source)}")
            while True:
                selection = input("Chọn số file (ví dụ 1,3,5-8; nhập 0 để quay lại): ").strip()
                if selection == "0":
                    selected = []
                    break
                try:
                    selected = select_files(selection, files)
                    break
                except ValueError:
                    print(f"Chọn số từ 1 đến {len(files)}, ví dụ 1,3,5-8. Không dùng khoảng giảm dần.")
            if not selected:
                continue
            print("\nCác file sẽ chạy:")
            for path in selected:
                print(f"  - {path.relative_to(source)}")

        print("\n1. Chỉ chạy file chưa có kết quả (mặc định)\n2. Chạy lại tất cả file đã chọn")
        while True:
            mode = input("Chọn chế độ [1]: ").strip() or "1"
            if mode in ("1", "2"):
                break
            print("Nhập 1 hoặc 2.")

        result = ROOT / "result"
        result.mkdir(parents=True, exist_ok=True)
        output = result / "data"
        gpu_python = ROOT / ".venv-dots-gpu" / "bin" / "python"
        command = [str(gpu_python) if gpu_python.exists() else sys.executable, str(ROOT / "thesis.py"),
                   "--pdf-dir", str(source), "--output-dir", str(output),
                   "--debug-dir", str(result / "debug_text"),
                   "--ocr-cache", str(ROOT / "ocr_cache_dots_mocr"),
                   "--ocr-backend", "direct", "--workers", "1", "--ocr-threads", "1"]
        if force_ocr:
            command.append("--force-ocr")
        if mode == "2":
            command.append("--rerun")
        command.extend(["--files", *(str(path) for path in selected)])
        print(f"\nĐã xử lý 0/{len(selected)} file. Đang khởi chạy dots.mocr...", flush=True)
        print(f"CSV và SQLite sẽ cập nhật sau mỗi PDF: {output}", flush=True)
        completed = subprocess.run(command)
        if completed.returncode == 0:
            print(f"\nKết quả: {output}\nXem Extraction_Logs.csv để kiểm tra từng file.")
        else:
            print(f"\nLần chạy dừng với mã lỗi {completed.returncode}. Xem thông báo phía trên.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trích xuất BCTC bằng text PDF và dots.mocr GPU")
    parser.add_argument("--force-ocr", action="store_true", help="ép dots.mocr OCR mọi trang thay vì dùng chữ có sẵn")
    args = parser.parse_args()
    try:
        main(force_ocr=args.force_ocr)
    except EOFError:
        print("\nĐã thoát.")
    except KeyboardInterrupt:
        print("\nĐã dừng. SQLite và CSV giữ kết quả các PDF đã xử lý xong; PDF đang chạy có thể chưa được lưu.")
        sys.exit(130)
