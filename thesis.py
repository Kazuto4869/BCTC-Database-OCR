#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRÍCH XUẤT DỮ LIỆU BCTC KIỂM TOÁN HỢP NHẤT (PDF) -> CSV + SQLITE
=========================================================
Đầu vào : thư mục chứa các file  [Mã_DN]_[Năm].pdf  (vd: VIC_2021.pdf)
Đầu ra  : thư mục result (hoặc --output-dir), chứa:
            1) Corporate_Bank_Loans.csv (chi tiết vay từ Thuyết minh)
            2) Extraction_Logs.csv      (trạng thái / cảnh báo từng file)
            3) extraction.sqlite3      (lưu bền vững từng PDF trên ổ cứng)
          (Các mã số CĐKT 320/338 vẫn được đọc nội bộ để đối chiếu tổng nợ vay,
           nhưng KHÔNG xuất sheet tài chính riêng.)

OCR cho file scan: dùng dots.mocr chạy qua vLLM (OpenAI-compatible API).
  1) Khởi động server (GPU):
       vllm serve dots-studio/dots.mocr --trust-remote-code --served-model-name model \
            --gpu-memory-utilization 0.9 --chat-template-content-format string
  2) Chạy script:
       pip install pymupdf pdfplumber thefuzz requests
       python thesis.py --pdf-dir ./Source --ocr-url http://localhost:8000/v1 --workers 1 --force-ocr
  Trang nào có text-layer thì đọc trực tiếp (nhanh); trang không có text -> render ảnh -> dots.mocr.
  * Bảng từ dots.mocr được đọc THEO CỘT (HTML -> lưới ô, nhận colspan/rowspan, chọn cột 'Số cuối năm/31/12/N').
  * Thuyết minh dạng văn xuôi: bộ bắt câu 'khoản vay ... tại <Ngân hàng> ... dư nợ ... là <số tiền>' (cờ NARRATIVE).
  * Tự xoay trang: OCR bình thường, nếu kết quả rác thì thử xoay 90/270/180 (--auto-rotate score|landscape|off).
  * Chỉ dùng server dots.mocr đã cấu hình; không gọi Gemini hay API dự phòng.
  * Khoản vay ngoại tệ (USD/EUR...): tự tìm tỷ giá trong BCTC / từ bảng 2 cột, hoặc --fx / --fx-file.
  Kết quả OCR từng trang được cache (--ocr-cache) nên chạy lại không phải OCR lại.

Thiết kế tóm tắt
----------------
* Đọc text bằng PyMuPDF (nhanh, dựng lại dòng theo toạ độ y của từng từ);
  nếu không đủ text -> thử pdfplumber; vẫn rỗng -> [SCANNED_PDF].
* Tìm trang CĐKT / BCKQKD, lấy giá trị theo MÃ SỐ (270, 300, 320, 338, 400, 50, 23).
* Quét các mục Thuyết minh "Vay và nợ thuê tài chính / Vay ngắn hạn / Vay dài hạn /
  Trái phiếu", tách từng dòng bên cho vay + số dư cuối năm.
* Khớp tên ngân hàng theo BANK_MAP (chuỗi dài trước, ticker ngắn sau).
* Đối chiếu tổng vay bóc được với (Mã 320 + Mã 338) -> OK / MISMATCH (>5%).

LƯU Ý: định dạng Thuyết minh của mỗi DN khác nhau rất nhiều. Hãy chạy thử 5-10 file,
xem Extraction_Logs.csv, rồi tinh chỉnh các hằng số/regex ở phần CẤU HÌNH nếu cần.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter, defaultdict, deque
from statistics import median
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from itertools import islice
from html.parser import HTMLParser
from pathlib import Path

from result_store import ResultStore

try:
    import pymupdf as fitz  # PyMuPDF (tên mới)
except ImportError:  # pragma: no cover
    try:
        import fitz  # PyMuPDF (tên cũ)
    except ImportError:
        fitz = None
try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None
try:
    import requests
except ImportError:  # pragma: no cover
    requests = None
try:
    from thefuzz import fuzz
except ImportError:  # pragma: no cover
    fuzz = None

# =============================================================================
# CẤU HÌNH
# =============================================================================
BANK_MAP = {
    "VCB": ["Ngân hàng TMCP Ngoại thương Việt Nam", "Ngoại thương", "Vietcombank", "VCB"],
    "CTG": ["Ngân hàng TMCP Công thương Việt Nam", "Công thương", "VietinBank", "CTG"],
    "BID": ["Ngân hàng TMCP Đầu tư và Phát triển Việt Nam", "Đầu tư và Phát triển", "BIDV", "BID"],
    "AGR": ["Ngân hàng Nông nghiệp và Phát triển Nông thôn Việt Nam", "Nông nghiệp", "Agribank", "AGRIBANK"],
    "TCB": ["Ngân hàng TMCP Kỹ thương Việt Nam", "Kỹ thương", "Techcombank", "TCB"],
    "MBB": ["Ngân hàng TMCP Quân Đội", "Quân Đội", "MBBank", "MB", "MBB"],
    "VPB": ["Ngân hàng TMCP Việt Nam Thịnh Vượng", "Việt Nam Thịnh Vượng", "VPBank", "VPB"],
    "HDB": ["Ngân hàng TMCP Phát triển Thành phố Hồ Chí Minh", "Phát triển TP.HCM", "HDBank", "HDB"],
    "STB": ["Ngân hàng TMCP Sài Gòn Thương Tín", "Sài Gòn Thương Tín", "Sacombank", "STB"],
    "SHB": ["Ngân hàng TMCP Sài Gòn - Hà Nội", "Sài Gòn - Hà Nội", "SHB"],
    "OCB": ["Ngân hàng TMCP Phương Đông", "Phương Đông", "OCB"],
    "MSB": ["Ngân hàng TMCP Hàng Hải Việt Nam", "Hàng Hải", "Maritime Bank", "MSB"],
    "VIB": ["Ngân hàng TMCP Quốc Tế Việt Nam", "Quốc Tế", "VIB"],
    "TPB": ["Ngân hàng TMCP Tiên Phong", "Tiên Phong", "TPBank", "TPB"],
    "EIB": ["Ngân hàng TMCP Xuất Nhập Khẩu Việt Nam", "Xuất Nhập Khẩu", "Eximbank", "EIB"],
    "LPB": ["Ngân hàng TMCP Bưu điện Liên Việt", "Ngân hàng TMCP Lộc Phát Việt Nam", "LienVietPostBank", "LPBank", "LPB"],
    "SSB": ["Ngân hàng TMCP Đông Nam Á", "Đông Nam Á", "SeABank", "SSB"],
    "ACB": ["Ngân hàng TMCP Á Châu", "Á Châu", "ACB"],
    "BAB": ["Ngân hàng TMCP Bắc Á", "Bắc Á", "BacABank", "BAB"],
    "ABB": ["Ngân hàng TMCP An Bình", "An Bình", "ABBANK", "ABB"],
    "NAB": ["Ngân hàng TMCP Nam Á", "Nam Á", "NamABank", "NAB"],
}

MISMATCH_TOL = 0.05          # lệch > 5% => [MISMATCH]
BALANCE_TOL = 0.005          # kiểm tra 270 = 300 + 400
UNMATCHED_SHARE_WARN = 0.5   # >50% dư nợ là bên cho vay không có trong từ điển => CHECK_MANUAL
MAX_SEG_LINES = 160          # số dòng tối đa của 1 đoạn thuyết minh vay
FUZZY_THRESHOLD = 93

OK, SCANNED, MISMATCH, CHECK = "[OK]", "[SCANNED_PDF]", "[MISMATCH]", "[CHECK_MANUAL]"
LT_LOAN, ST_LOAN, BOND = "Vay dài hạn", "Vay ngắn hạn", "Trái phiếu"

FILE_RE = re.compile(r"^([A-Za-z0-9]+)_(20\d{2})\.pdf$", re.I)

# =============================================================================
# TIỆN ÍCH VĂN BẢN
# =============================================================================
DASHES = {"-", "–", "−", "—"}


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("đ", "d").replace("Đ", "D")


def norm(s: str) -> str:
    """lower + bỏ dấu + chuẩn hoá gạch nối + rút gọn 'Thương mại cổ phần' -> 'tmcp'
    + xoá khoảng trắng thừa."""
    s = unicodedata.normalize("NFC", s or "")
    s = strip_accents(s).lower()
    s = s.replace("–", "-").replace("—", "-").replace("‑", "-")
    s = re.sub(r"\s*-\s*", " - ", s)
    s = re.sub(r"\bthuong mai co phan\b", "tmcp", s)
    s = re.sub(r"\bnh\s*tmcp\b|\bnhtmcp\b", "ngan hang tmcp", s)
    return re.sub(r"\s+", " ", s).strip()


NUM_TOKEN = re.compile(r"^\(?[-−–]?\d[\d.,]*\)?$|^[-−–]$")
NOTE_TOKEN = re.compile(r"^\(?[IVXivx]+\.\d{1,2}(?:\.\d{1,2})?[a-zA-Z]?(?:,[a-zA-Z])*\)?$")
AMOUNT_RE = re.compile(r"^\(?[-−–]?(?:\d{1,3}(?:[.,]\d{3})+|\d{3,})\)?$")
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")


def is_amount(tok: str) -> bool:
    return bool(AMOUNT_RE.match(tok)) and not YEAR_RE.match(tok.strip("()"))


def parse_number(tok: str):
    """'(123.456)' / '-123.456' -> -123456 ; '1.234.567' -> 1234567 ; '-' -> 0."""
    t = tok.strip().replace("−", "-").replace("–", "-")
    if t in ("", "-", "0"):
        return 0
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    if t.startswith("-"):
        neg, t = True, t[1:]
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d+", t):       # 1.234.567,89
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d+", t):     # 1,234,567.89
        t = t.replace(",", "")
    else:
        t = re.sub(r"[.,]", "", t)
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


# =============================================================================
# ĐỌC PDF
# =============================================================================
def words_to_lines(words, tol=3.0):
    """Dựng lại dòng từ danh sách từ (x0,y0,x1,y1,text,...) theo toạ độ y."""
    ws = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines, cur, cy = [], [], None
    for w in ws:
        y = (w[1] + w[3]) / 2
        if cy is None or abs(y - cy) <= tol:
            cur.append(w)
            cy = y if cy is None else (cy * (len(cur) - 1) + y) / len(cur)
        else:
            lines.append(cur)
            cur, cy = [w], y
    if cur:
        lines.append(cur)
    return [" ".join(x[4] for x in sorted(l, key=lambda w: w[0])) for l in lines]


def _dominant_dir_matrix(page):
    """Hướng chữ chiếm đa số trong trang (từ 'dir' của từng line) -> ma trận xoay chữ về nằm ngang."""
    try:
        d = page.get_text("dict")
    except Exception:
        return None
    cnt = {}
    for b in d.get("blocks", []):
        for l in b.get("lines", []):
            dx, dy = l.get("dir", (1, 0))
            n = sum(len(sp.get("text", "")) for sp in l.get("spans", []))
            key = (round(dx), round(dy))
            cnt[key] = cnt.get(key, 0) + n
    if not cnt:
        return None
    key = max(cnt, key=cnt.get)
    return {(0, -1): fitz.Matrix(0, 1, -1, 0, 0, 0),
            (0, 1): fitz.Matrix(0, -1, 1, 0, 0, 0)}.get(key)


def page_lines(page):
    """Dựng dòng của 1 trang có text-layer. Nếu chữ chạy dọc (nội dung bị xoay 90/270 độ trong trang
    landscape) thì xoay toạ độ từ về nằm ngang trước khi gom dòng."""
    words = page.get_text("words")
    if len(words) >= 10:
        T = _dominant_dir_matrix(page)
        if T is not None:
            w2 = []
            for w in words:
                r = fitz.Rect(w[:4]) * T
                w2.append((r.x0, r.y0, r.x1, r.y1, w[4]))
            return words_to_lines(w2)
    return words_to_lines(words)


def read_fitz(path):
    doc = fitz.open(path)
    try:
        return [page_lines(p) for p in doc]
    finally:
        doc.close()


def read_pdfplumber(path, max_pages=None):
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, p in enumerate(pdf.pages):
            if max_pages is not None and i >= max_pages:
                break
            txt = p.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
            pages.append([l for l in txt.split("\n") if l.strip()])
    return pages


def text_rich(pages, min_chars=150, min_ratio=0.2):
    if not pages:
        return False
    rich = sum(1 for p in pages if sum(len(l) for l in p) >= min_chars)
    return rich / len(pages) >= min_ratio


# -----------------------------------------------------------------------------
# OCR: dots.mocr (vLLM local, không dùng API dự phòng)
# -----------------------------------------------------------------------------
MIN_PAGE_CHARS = 80    # trang có < 80 ký tự text-layer => coi là trang scan, cần OCR
IMG_PREFIX = "<|img|><|imgpad|><|endofimg|>"
INK_MIN = 0.015        # tỉ lệ điểm ảnh "mực" tối thiểu để coi trang là có nội dung (không phải trang trắng)
QUALITY_MIN = 0.10     # điểm chất lượng OCR tối thiểu; thấp hơn => thử xoay trang

OCR_PROMPT = """Please output the layout information from the PDF image, including each layout element's bbox, its category, and the corresponding text content within the bbox.

1. Bbox format: [x1, y1, x2, y2]

2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', 'Text', 'Title'].

3. Text Extraction & Formatting Rules:
    - Picture: For the 'Picture' category, the text field should be omitted.
    - Formula: Format its text as LaTeX.
    - Table: Format its text as HTML.
    - All Others (Text, Title, etc.): Format their text as Markdown.

4. Constraints:
    - The output text must be the original text from the image, with no translation.
    - All layout elements must be sorted according to human reading order.

5. Final Output: The entire output must be a single JSON object.
"""

class Page(list):
    """Danh sách dòng của 1 trang. Với trang OCR còn có .blocks giữ cấu trúc:
    ("text", [dòng...]) hoặc ("table", grid, rep) để đọc bảng theo cột."""
    blocks = None


def _int(v, d=1):
    try:
        return max(1, min(50, int(re.sub(r"\D", "", str(v)))))
    except Exception:
        return d


class _TableParser(HTMLParser):
    """HTML <table> -> lưới ô (giữ ô rỗng, mở rộng colspan/rowspan). rep[r][c]=True nếu ô là bản sao do gộp ô."""

    def __init__(self):
        super().__init__()
        self.grid, self.rep = [], []
        self._row = self._rrep = self._cell = None
        self._cs = self._rs = 1
        self._spans = {}

    def _fill(self):
        while self._row is not None and len(self._row) in self._spans:
            c = len(self._row)
            rem, txt = self._spans[c]
            self._row.append(txt)
            self._rrep.append(True)
            if rem <= 1:
                del self._spans[c]
            else:
                self._spans[c] = [rem - 1, txt]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row, self._rrep = [], []
            self._fill()
        elif tag in ("td", "th"):
            if self._row is None:
                self._row, self._rrep = [], []
            self._fill()
            self._cell = []
            self._cs, self._rs = _int(a.get("colspan")), _int(a.get("rowspan"))
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            txt = " ".join("".join(self._cell).split())
            c0 = len(self._row)
            for k in range(self._cs):
                self._row.append(txt)
                self._rrep.append(k > 0)
                if self._rs > 1:
                    self._spans[c0 + k] = [self._rs - 1, txt]
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._fill()
            self.grid.append(self._row)
            self.rep.append(self._rrep)
            self._row = self._rrep = None

    def handle_data(self, d):
        if self._cell is not None:
            self._cell.append(d)


def html_table_to_grid(html: str):
    p = _TableParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    grid, rep = p.grid, p.rep
    if not grid:
        return [], []
    w = max(len(r) for r in grid)
    grid = [r + [""] * (w - len(r)) for r in grid]
    rep = [r + [True] * (w - len(r)) for r in rep]
    return grid, rep


def grid_to_lines(grid, rep):
    """Mỗi hàng bảng -> 1 dòng text (bỏ ô bản sao do colspan/rowspan) - dùng cho phần đọc theo dòng."""
    out = []
    for row, rr in zip(grid, rep):
        cells = [c for c, r in zip(row, rr) if c and not r]
        if cells:
            out.append(" ".join(cells))
    return out


def _md_to_lines(t: str):
    out = []
    for l in t.splitlines():
        l = re.sub(r"[*#`]+", "", l).replace("|", " ")
        l = re.sub(r"\s+", " ", l).strip()
        if l and not re.fullmatch(r"[-: ]+", l):
            out.append(l)
    return out


def valid_layout_json(raw):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    try:
        return isinstance(json.loads(text), (list, dict))
    except (json.JSONDecodeError, TypeError):
        return False


def _parse_elements(raw: str):
    """Phản hồi OCR -> [(category, text)]. Chịu được JSON bị cắt cụt / bọc ```json."""
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    data = None
    try:
        data = json.loads(txt)
    except Exception:
        m = re.search(r"\[.*\]", txt, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if isinstance(data, dict):
        data = data.get("elements") or data.get("layout") or [data]
    if isinstance(data, list):
        return [(str(el.get("category", "")), el.get("text") or "") for el in data if isinstance(el, dict)]
    frags = re.findall(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', txt)     # JSON cụt: vớt từng trường text
    if frags:
        out = []
        for f in frags:
            try:
                f = json.loads('"' + f + '"')
            except Exception:
                pass
            out.append(("Text", f))
        return out
    return [("Text", txt)]


def ocr_response_to_page(raw: str) -> Page:
    page = Page()
    page.blocks = []
    for cat, t in _parse_elements(raw):
        if cat in ("Picture", "Page-footer") or not str(t).strip():
            continue
        if "<table" in t.lower():
            grid, rep = html_table_to_grid(t)
            if grid:
                page.blocks.append(("table", grid, rep))
                page.extend(grid_to_lines(grid, rep))
        else:
            ls = _md_to_lines(t)
            if ls:
                page.blocks.append(("text", ls))
                page.extend(ls)
    return page


def ocr_response_to_lines(raw: str):          # tương thích ngược
    return list(ocr_response_to_page(raw))


LEXICON = set("""va cua cong ty ngan hang nam so tai san von vay no thue chi phi doanh thu tien gia tri cuoi dau ma
thuyet minh tong cac khoan phai tra mot cho trong duoc bao cao ket qua kinh loi nhuan co phan huu han""".split())


def ocr_quality(page):
    """(điểm, số_token_chữ). Điểm = tỉ lệ từ thuộc từ vựng tài chính-kế toán tiếng Việt (đã bỏ dấu).
    Trang bị xoay sai cho ra chuỗi rác -> điểm rất thấp."""
    toks = re.findall(r"[a-z]+", norm(" ".join(page)))
    if not toks:
        return 0.0, 0
    return sum(t in LEXICON for t in toks) / len(toks), len(toks)


# ---- render + xoay ----
def render_png(page, dpi, rot=0):
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    if rot:
        mat = mat.prerotate(rot)
    return page.get_pixmap(matrix=mat, alpha=False).tobytes("png")


def ink_ratio(page):
    if np is None:
        return 1.0
    pix = page.get_pixmap(dpi=40, colorspace=fitz.csGRAY, alpha=False)
    a = np.frombuffer(pix.samples, dtype=np.uint8)
    return float((a < 160).mean())


# ---- backends ----
_DOWN_UNTIL = {}


class ApiError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:220]}")
        self.status = status


class OCRContentError(RuntimeError):
    """The image needs another rendering, rather than a network retry."""


def _check(r):
    if r.status_code >= 400:
        raise ApiError(r.status_code, r.text.replace("\n", " "))


def _call_openai_compat(png, be):
    url = be["url"].rstrip("/") + "/chat/completions"
    b64 = base64.b64encode(png).decode()
    text = (IMG_PREFIX if be.get("prefix") else "") + be["prompt"]
    headers = {"Authorization": f"Bearer {be['key']}"} if be.get("key") else {}
    payload = {"model": be.get("model", "model"),
               "messages": [{"role": "user", "content": [
                   {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                   {"type": "text", "text": text}]}],
               "max_tokens": be.get("max_tokens", 16000), "temperature": 0.0, "top_p": 1.0}
    r = requests.post(url, json=payload, headers=headers, timeout=be.get("timeout", 300))
    _check(r)
    choice = r.json()["choices"][0]
    if choice.get("finish_reason") == "length":
        raise OCRContentError("OCR bị cắt do hết token; không lưu kết quả trang chưa hoàn chỉnh")
    raw = choice["message"]["content"]
    if not valid_layout_json(raw):
        raise OCRContentError("OCR trả JSON không hợp lệ; không lưu kết quả trang lỗi")
    return raw


def _ocr_dispatch(png, cfg):
    """Gọi dots.mocr. Backend lỗi liên tiếp bị tạm nghỉ 2 phút."""
    bes = cfg.get("backends") or []
    if not bes:
        raise RuntimeError("không có backend OCR khả dụng")
    now = time.time()
    order = [b for b in bes if _DOWN_UNTIL.get(b["name"], 0) <= now] or bes
    last = None
    for be in order:
        for attempt in range(be.get("retries", 3)):
            try:
                return _call_backend(png, be), be["name"]
            except OCRContentError:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                st = getattr(e, "status", None)
                if st in (400, 401, 403, 404):          # sai key / sai tên model / bị chặn: thử lại vô ích
                    break
                time.sleep(25 * (attempt + 1) if st == 429 else min(2 * (2 ** attempt), 16))   # 429 = vượt giới hạn tốc độ
        _DOWN_UNTIL[be["name"]] = time.time() + 120
    raise last


def _call_backend(png, be):
    if be.get("kind") != "direct":
        return _call_openai_compat(png, be)
    from dots_mocr_gpu import recognize
    failure = None
    try:
        raw = recognize(png, be)
    except ValueError as error:
        failure = str(error)
    if failure is not None:
        raise OCRContentError(failure)
    if not valid_layout_json(raw):
        raise OCRContentError("OCR trả JSON không hợp lệ; không lưu kết quả trang lỗi")
    return raw


def ocr_one_page(path, i, cfg):
    """OCR 1 trang; tự thử xoay nếu kết quả có vẻ rác. Trả về {'raw','rot','backend'}."""
    with fitz.open(path) as doc:
        page = doc[i]
        mode = cfg.get("rotate", "score")
        land = page.rect.width > page.rect.height
        base = 90 if (mode == "landscape" and land) else 0      # 'landscape': luôn xoay trang ngang thành đứng
        order = [base] + ([90, 270, 180] if mode == "score" else [])
        best = None
        for rot in order:
            dpi = cfg.get("dpi", 200)
            retry_error = None
            try:
                raw, backend = _ocr_dispatch(render_png(page, dpi, rot), cfg)
            except OCRContentError as error:
                retry_error = str(error)
            if retry_error is not None:
                # Keep the entire table; cropping can lose its column headers.
                retry_dpi = 150 if dpi != 150 else 200
                print(f"    {Path(path).name}: trang {i + 1}: {retry_error}; thử ảnh {retry_dpi} DPI", flush=True)
                retry_cfg = dict(cfg, backends=[dict(be, max_pixels=524288) if be.get("kind") == "direct" else be
                                               for be in cfg.get("backends", [])])
                raw, backend = _ocr_dispatch(render_png(page, retry_dpi, rot), retry_cfg)
            sc, nt = ocr_quality(ocr_response_to_page(raw))
            score = sc if nt >= 8 else (0.0 if nt == 0 else 1.0)       # 1-7 từ: không đủ kết luận -> coi là ổn
            if best is None or score > best[0]:
                best = (score, raw, rot, backend)
            if mode != "score":
                break
            bad = (nt == 0 and ink_ratio(page) >= INK_MIN) or (nt >= 8 and sc < QUALITY_MIN)
            if not bad:
                break
        return {"raw": best[1], "rot": best[2], "backend": best[3]}

def ocr_pages(path, idxs, cfg, native=None):
    """OCR các trang idxs. Trả về (dict idx->Page, list idx lỗi, meta). Cache RAW theo trang."""
    with open(path, "rb") as source:
        fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
    base = Path(cfg["cache_dir"]) / Path(path).stem
    cdir = base / fingerprint
    legacy = list(base.glob("p*.json"))
    has_versions = any(p.is_dir() and re.fullmatch(r"[a-f0-9]{64}", p.name) for p in base.glob("*"))
    cdir.mkdir(parents=True, exist_ok=True)
    # Adopt the old cache only if the PDF predates it; keep the original files.
    if legacy and not has_versions and Path(path).stat().st_mtime_ns <= min(p.stat().st_mtime_ns for p in legacy):
        for page_cache in legacy:
            shutil.copy2(page_cache, cdir / page_cache.name)
    out, todo, meta = {}, [], {"rotated": 0, "backends": Counter(), "errors": Counter(), "invalid_json": [], "native": []}

    def _take(i, d):
        if not valid_layout_json(d["raw"]):
            meta["invalid_json"].append(i + 1)
        out[i] = ocr_response_to_page(d["raw"])
        if d.get("rot"):
            meta["rotated"] += 1
        if d.get("backend"):
            meta["backends"][d["backend"]] += 1

    for i in idxs:
        cp = cdir / f"p{i:04d}.json"
        if cp.exists():
            try:
                cached = json.loads(cp.read_text(encoding="utf-8"))
                if valid_layout_json(cached["raw"]) or not cfg.get("backends"):
                    _take(i, cached)
                    continue
            except Exception:
                pass
        if native is not None and sum(len(line) for line in native[i]) >= MIN_PAGE_CHARS:
            out[i] = native[i]
            meta["native"].append(i)
            continue
        todo.append(i)
    failed, done_n, t_start = [], 0, time.time()
    print(f"    {Path(path).name}: cache {len(out) - len(meta['native'])}/{len(idxs)} trang; "
          f"đọc chữ PDF {len(meta['native'])} trang; cần OCR {len(todo)} trang", flush=True)
    if todo:
        chunk = max(1, cfg.get("threads", 8) * 3)
        with ThreadPoolExecutor(cfg.get("threads", 8)) as executor:
            for c in range(0, len(todo), chunk):
                futs = {executor.submit(ocr_one_page, path, i, cfg): i for i in todo[c:c + chunk]}
                for fu in as_completed(futs):
                    i = futs[fu]
                    done_n += 1
                    try:
                        d = fu.result()
                        (cdir / f"p{i:04d}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                        _take(i, d)
                    except Exception as error:  # noqa: BLE001
                        failed.append(i)
                        meta["errors"][str(error)[:230]] += 1
                        print(f"[!] {Path(path).name}: trang {i + 1} lỗi: {error}", flush=True)
                    if done_n % 10 == 0 or done_n == len(todo) or any(be.get("kind") == "direct" for be in cfg.get("backends", [])):
                        el = time.time() - t_start
                        print(f"    {Path(path).name}: OCR {done_n}/{len(todo)} trang "
                              f"(thành công {done_n - len(failed)}, lỗi {len(failed)}; "
                              f"{el:.0f}s, còn ~{el / done_n * (len(todo) - done_n):.0f}s)", flush=True)
    if failed:
        top = meta["errors"].most_common(1)[0][0] if meta["errors"] else "?"
        print(f"[!] {Path(path).name}: OCR lỗi {len(failed)}/{len(todo)} trang. Lỗi thường gặp nhất: {top}", flush=True)
    return out, failed, meta


def load_pages(path, cfg=None, force_ocr=False):
    """Trả về (pages, engine, info). pages=[] nếu không đọc được gì (scan mà không OCR được).
    Trang có text-layer -> đọc trực tiếp; trang rỗng (hoặc force_ocr) -> OCR."""
    info = {"ocr_pages": 0, "ocr_failed": 0, "rotated": 0, "backends": {}, "errors": [], "invalid_json": []}
    native = []
    if fitz is not None:
        try:
            native = read_fitz(path)
        except Exception:
            native = []
    elif pdfplumber is not None:
        try:
            native = read_pdfplumber(path)
        except Exception:
            native = []
    if not native:
        return [], "none", info

    n = len(native)
    need = list(range(n)) if force_ocr or (cfg and cfg.get("enabled")) else [i for i, p in enumerate(native) if sum(len(l) for l in p) < MIN_PAGE_CHARS]
    if need and cfg and cfg.get("enabled") and fitz is not None:
        got, failed, meta = ocr_pages(path, need, cfg, native=None if force_ocr else native)
        for i, pg in got.items():
            native[i] = pg
        info.update(ocr_pages=len(got) - len(meta.get("native", [])), ocr_failed=len(failed), rotated=meta["rotated"],
                    backends=dict(meta["backends"]), errors=[k for k, _ in meta["errors"].most_common(2)],
                    invalid_json=meta["invalid_json"])
        engine = "fitz" if not info["ocr_pages"] else ("ocr" if info["ocr_pages"] == n else "fitz+ocr")
    else:
        engine = "fitz" if fitz is not None else "pdfplumber"
    if not text_rich(native):
        return [], engine, info
    return native, engine, info


# =============================================================================
# KHỚP TÊN NGÂN HÀNG
# =============================================================================
NONBANK_RE = re.compile(r"chung khoan|securities|bao hiem|insurance|quan ly quy|cho thue tai chinh|leasing|cong ty tai chinh")


def _build_aliases():
    out = []
    for tk, names in BANK_MAP.items():
        for nm in names:
            a = norm(nm)
            ascii_only = all(ord(c) < 128 for c in nm)
            if a.startswith("ngan hang"):
                kind = "legal"
            elif len(nm) <= 4 and nm.isupper() and ascii_only and " " not in nm:
                kind = "ticker"
            elif not ascii_only:
                kind = "short_vn"
            else:
                kind = "brand"
            if kind == "ticker":
                rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(nm) + r"(?![A-Za-z0-9])")  # phân biệt hoa/thường
            else:
                rx = re.compile(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])")
            out.append((a, tk, kind, rx))
    prio = {"legal": 0, "brand": 1, "short_vn": 2, "ticker": 3}
    out.sort(key=lambda x: (-len(x[0]), prio[x[2]]))
    return out


ALIASES = _build_aliases()
LEGAL_NAMES = [(norm(n), tk) for tk, names in BANK_MAP.items() for n in names if norm(n).startswith("ngan hang")]


def match_bank(raw: str):
    """Trả về ticker NH nếu khớp BANK_MAP, ngược lại None.
    Thứ tự: tên pháp lý/dài trước -> thương hiệu -> tên ngắn (cần chữ 'ngân hàng') -> ticker."""
    if not raw:
        return None
    n = norm(raw)
    has_bank_word = "ngan hang" in n or "bank" in n
    if NONBANK_RE.search(n) and "ngan hang" not in n:
        return None
    ctx = bool(re.search(r"ngan hang|bank|chi nhanh|\bcn\b|\bnh\b|tmcp", n))
    first_tok = raw.strip().split()[0].strip("()-:.") if raw.strip() else ""
    for a, tk, kind, rx in ALIASES:
        if kind == "ticker":
            if rx.search(raw) and (ctx or first_tok == a.upper()):
                return tk
        elif kind == "short_vn":
            if has_bank_word and rx.search(n):
                return tk
        else:
            if rx.search(n):
                return tk
    if fuzz is not None and "ngan hang" in n:
        best, best_tk = 0, None
        for a, tk in LEGAL_NAMES:
            sc = fuzz.partial_ratio(a, n)
            if sc > best:
                best, best_tk = sc, tk
        if best >= FUZZY_THRESHOLD:
            return best_tk
    return None


def is_generic(ln: str) -> bool:
    words = re.findall(r"[a-z]+", ln)
    if "khac" in words and re.search(r"ngan hang|to chuc|tctd|vay|ca nhan|doi tuong|ben ", ln):
        return True
    s = strip_prefix(ln)
    return bool(re.fullmatch(r"(?:vay )?(?:cac )?(?:ngan hang|to chuc tin dung)(?: thuong mai)?(?: va (?:ca nhan|to chuc))?", s))


# =============================================================================
# CĐKT / BCKQKD THEO MÃ SỐ
# =============================================================================
CODE_LINE_RE = re.compile(r"\s(\d{2,3})\s+(?:[IVXivx]*\.?\d{1,2}(?:\.\d{1,2})?\s+)?\(?-?\d{1,3}(?:[.,]\d{3})+\)?(?:\s|$)")


def count_code_lines(lines):
    return sum(1 for l in lines if CODE_LINE_RE.search(" " + l) or
               re.match(r"^\s*\d{2,3}\s+.*[A-Za-zÀ-ỹ].*?\d{1,3}(?:[.,]\d{3})+", l))


def find_stmt_start(pages, key, from_idx=0, min_codes=6):
    for i in range(from_idx, len(pages)):
        head = norm(" ".join(pages[i][:30]))
        if re.search(r"so lieu truoc dieu chinh|so lieu sau dieu chinh|cac dieu chinh", head):
            continue
        if key in head and count_code_lines(pages[i]) >= min_codes:
            return i
    return None


def detect_header(window: str, year: int):
    """Trả về (closing_first, movement) nếu nhận ra header cột; ngược lại None."""
    c_keys = ["cuoi nam", "cuoi ky", f"31/12/{year}", "nam nay"]
    d_keys = ["dau nam", "dau ky", f"01/01/{year}", f"31/12/{year - 1}", "nam truoc"]
    ci = [window.find(k) for k in c_keys if window.find(k) >= 0]
    di = [window.find(k) for k in d_keys if window.find(k) >= 0]
    if not ci or not di:
        return None
    movement = bool(re.search(r"\btang\b", window) and re.search(r"\bgiam\b", window))
    return (min(ci) < min(di), movement)


def parse_code_value(line: str, code: str):
    """Tìm token == mã số, theo sau chỉ là (thuyết minh) + số liệu. Trả về token giá trị năm nay."""
    toks = line.split()
    for i, t in enumerate(toks):
        if t != code or i + 1 >= len(toks):
            continue
        if i >= 1 and AMOUNT_RE.match(toks[i - 1]):   # token trước là số tiền -> không phải mã số
            continue
        tail = toks[i + 1:]
        # Some reports put the code before the account description (EY / DXG).
        if i == 0 and not all(NUM_TOKEN.match(x) or NOTE_TOKEN.match(x) for x in tail):
            j = len(toks)
            while j > 1 and (NUM_TOKEN.match(toks[j - 1]) or NOTE_TOKEN.match(toks[j - 1])):
                j -= 1
            tail = toks[j:]
            if not tail or not any(AMOUNT_RE.match(x) for x in tail):
                continue
        if not all(NUM_TOKEN.match(x) or NOTE_TOKEN.match(x) for x in tail):
            continue
        return tail
    return None


def pick_statement_value(tail, closing_first=True):
    vals = list(tail)
    if len(vals) >= 3:                       # [thuyết minh, năm nay, năm trước]
        cur, prev = vals[-2], vals[-1]
    elif len(vals) == 2:
        cur, prev = vals[0], vals[1]
        if NOTE_TOKEN.match(cur):
            cur, prev = vals[1], None
    else:
        cur, prev = vals[0], None
    if not closing_first and prev is not None:
        cur = prev
    return parse_number(cur)


def extract_codes(pages, start, span, codes, year):
    out = {}
    for pi in range(start, min(len(pages), start + span)):
        hdr = detect_header(norm(" ".join(pages[pi][:40])), year)
        cf = hdr[0] if hdr else True
        for line in pages[pi]:
            for c in codes:
                if c in out:
                    continue
                tail = parse_code_value(line, c)
                if tail:
                    v = pick_statement_value(tail, cf)
                    if v is not None:
                        out[c] = v
    return out


def detect_unit(pages, hint_idx):
    """Đọc 'Đơn vị tính: ...' -> hệ số quy đổi về VND."""
    order = [i for i in hint_idx if i is not None] + list(range(min(25, len(pages))))
    seen = set()
    for i in order:
        if i in seen or i >= len(pages):
            continue
        seen.add(i)
        for line in pages[i][:60]:
            n = norm(line)
            for m in re.finditer(r"(?:don vi tinh|don vi tien te|don vi bao cao|dvt|don vi)\s*[:\-]?\s*\(?([a-z ]{2,30})", n):
                g = m.group(1)
                if re.search(r"\bty\b", g):
                    return 1_000_000_000, "Tỷ đồng"
                if re.search(r"\btrieu\b", g):
                    return 1_000_000, "Triệu đồng"
                if re.search(r"\bnghin\b|\bngan dong\b|\bngan vnd\b", g):
                    return 1_000, "Nghìn đồng"
                if re.search(r"\bdong\b|\bvnd\b", g):
                    return 1, "Đồng"
        # Read the statement's currency columns before unrelated annual-report highlights.
        for line in pages[i][:60]:
            n = norm(line)
            if n in ("vnd", "dong") or (re.search(r"\bvnd\b", n) and
                                               re.search(r"\b\d{1,2}/\d{1,2}/20\d{2}\b", n)):
                return 1, "Đồng"
    return 1, "KHÔNG_THẤY(mặc định Đồng)"


# =============================================================================
# THUYẾT MINH VAY
# =============================================================================
HEAD_RE = re.compile(
    r"^(?:[ivx]+\.\s*)?(?:(\d{1,2})[a-z]?\s*[\.\)\-]?\s+)?(?:thuyet minh\s*)?"
    r"(vay va no|vay ngan han|vay dai han|no thue tai chinh|trai phieu|vay$)"
)
NOTE_TITLE_RE = re.compile(
    r"von chu so huu|von dieu le|thue va cac khoan|chi phi phai tra|phai tra|du phong|doanh thu|gia von|"
    r"chi phi|loi nhuan|cac khoan phai|thong tin|nghiep vu|cong cu tai chinh|quan ly rui ro|tai san|hang ton kho|"
    r"tien va cac khoan|dau tu|su kien|bo phan"
)
TOTAL_RE = re.compile(
    r"^(?:cong|tong|total|so du|so dau|so cuoi|trong do|tru\b|giam\b|tang\b|chenh lech|phan loai|ket chuyen|"
    r"vay trong nam|tien tra|so tien (?:vay|da)|thanh toan|dieu chinh|danh gia|chi phi|lai\b|goc\b|tien lai|"
    r"da tra|so phat sinh|phat sinh)"
)
PERIOD_RE = re.compile(r"^(?:trong|tu|tren|duoi|den|qua|sau)\b.*\b(?:nam|thang|han)\b")
HEADER_LINE_RE = re.compile(
    r"cuoi nam|dau nam|cuoi ky|dau ky|gia tri|so co kha nang|^\d{2}/\d{2}/\d{4}|^vnd$|^dvt|mau so|^trang\b|^\d+$|^don vi|"
    r"thuyet minh cac bao cao|cac thuyet minh|^bao cao tai chinh"
)
AGENT_RE = re.compile(r"dai ly|tu van|bao lanh|dai dien nguoi so huu|luu ky|sap xep|to chuc tu van")
BUY_RE = re.compile(r"\b(mua|nam giu|cho vay)\b")


def strip_prefix(n: str) -> str:
    return re.sub(
        r"^(?:[\(\[]?(?:[a-z]|[ivx]{1,4}|\d{1,2}[a-z]?(?:\.\d{1,2})*)[\)\]\.]\s*|[-+•*]\s*)+", "", n
    ).strip()


def classify_heading(n: str):
    """Trả về (loan_type, is_current_portion_of_LT) nếu dòng là tiêu đề nhóm vay; ngược lại None."""
    s = re.sub(r"\([^)]*\)", "", strip_prefix(n)).strip()
    if s == "ngan han":
        return ST_LOAN, False
    if s == "dai han":
        return LT_LOAN, False
    w = len(s.split())
    if "den han tra" in s or "den han thanh toan" in s:
        return (BOND if "trai phieu" in s else ST_LOAN, True) if w <= 10 else None
    if "trai phieu" in s:
        return (BOND, False) if w <= 7 else None
    if w > 10:
        return None
    if re.search(r"\bngan han\b", s) and re.search(r"\b(vay|no)\b", s):
        return (ST_LOAN, False)
    if re.search(r"\bdai han\b", s) and re.search(r"\b(vay|no)\b", s):
        return (LT_LOAN, False)
    return None


def has_amount(line: str) -> bool:
    return any(is_amount(t) for t in line.split())


def is_next_note_heading(n: str, raw: str, no):
    if has_amount(raw) or HEAD_RE.match(n) or len(n) > 110:
        return False
    m = re.match(r"^(?:[ivx]+\.\s*)?(\d{1,2})\s*[\.\)]\s+[a-z]", n)
    if not m:
        return False
    if re.search(r"ngan hang|cong ty|vay", n):
        return False
    k = int(m.group(1))
    if no is not None and k == no + 1:
        return True
    return bool(NOTE_TITLE_RE.search(n))


def pick_closing(vals, closing_first, movement):
    n = len(vals)
    if n == 0:
        return None
    if n == 1:
        return vals[0]
    if movement:
        return vals[0] if closing_first else vals[-1]
    if closing_first:
        return vals[0]
    return vals[n // 2] if n % 2 == 0 else vals[-1]


def pick_closing_pos(n, closing_first, movement):
    """Vị trí (0..n-1) của cột 'cuối năm' trong n cột số, khi không đọc được tiêu đề cột."""
    if n <= 1:
        return 0
    if movement:
        return 0 if closing_first else n - 1
    if closing_first:
        return 0
    return n // 2 if n % 2 == 0 else n - 1


# =============================================================================
# NGOẠI TỆ
# =============================================================================
CCY_RE = re.compile(r"(?<![A-Za-z])(USD|EUR|JPY|CNY|GBP|AUD|SGD|CHF)(?![A-Za-z])")
FX_RANGE = {"USD": (18000, 35000), "EUR": (18000, 45000), "JPY": (100, 400)}
SCALE = {"ty": 1e9, "trieu": 1e6, "nghin": 1e3}


def detect_ccy(text):
    """Mã ngoại tệ xuất hiện trong text (None = coi là VND)."""
    m = CCY_RE.search(text or "")
    if m:
        return m.group(1)
    n = norm(text or "")
    if re.search(r"\bdo la my\b|\bdola my\b", n):
        return "USD"
    if re.search(r"\beuro\b", n):
        return "EUR"
    if re.search(r"\bnhan dan te\b|\byen\b", n):
        return "JPY" if "yen" in n else "CNY"
    return None


def ccy_from_norm(h):
    m = re.search(r"\b(usd|eur|jpy|cny|gbp|aud|sgd|chf)\b", h)
    return m.group(1).upper() if m else None


def parse_scaled(t):
    """'1.234' / '1,5' / '1.234.567,89' -> float (theo cách viết số của VN, có xét dấu thập phân)."""
    t = t.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", t):
        return float(t.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", t):
        return float(t.replace(",", ""))
    if re.fullmatch(r"\d+,\d+", t):
        return float(t.replace(",", "."))
    try:
        return float(t)
    except ValueError:
        return None


def detect_fx_rates(pages):
    """Tìm tỷ giá cuối kỳ ghi trong BCTC (vd '24.260 VND/USD', '1 USD = 24.260 VND'). Lấy giá trị xuất hiện nhiều nhất."""
    cnt = defaultdict(Counter)
    p1 = re.compile(r"(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?)\s*(?:vnd|dong)?\s*/\s*(usd|eur|jpy)\b")
    p2 = re.compile(r"\b1\s*(usd|eur|jpy)\s*(?:=|bang|tuong duong|~)\s*(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?)")
    for pg in pages:
        for line in pg:
            if not re.search(r"usd|eur|jpy", line, re.I):
                continue
            n = norm(line)
            if "binh quan" in n:
                continue
            for m in p1.finditer(n):
                num, c = m.group(1), m.group(2).upper()
                v = parse_scaled(num)
                if v and FX_RANGE[c][0] <= v <= FX_RANGE[c][1]:
                    cnt[c][round(v)] += 1
            for m in p2.finditer(n):
                c, num = m.group(1).upper(), m.group(2)
                v = parse_scaled(num)
                if v and FX_RANGE[c][0] <= v <= FX_RANGE[c][1]:
                    cnt[c][round(v)] += 1
    return {c: ctr.most_common(1)[0][0] for c, ctr in cnt.items() if ctr}


def resolve_fx(pages, year, implied, fx_cfg):
    """Thứ tự ưu tiên: --fx (tay) > tỷ giá ghi trong BCTC > tỷ giá ngầm từ bảng 2 cột (nguyên tệ & VND) > --fx-file."""
    fx_cfg = fx_cfg or {}
    out = {}
    for c, v in (fx_cfg.get("file", {}) or {}).items():
        r = (v or {}).get(str(year))
        if r:
            out[c] = (float(r), "fx-file")
    for c, lst in implied.items():
        if lst:
            out[c] = (float(median(lst)), "bảng(ngầm)")
    for c, v in detect_fx_rates(pages).items():
        out[c] = (float(v), "BCTC")
    for c, v in (fx_cfg.get("override", {}) or {}).items():
        out[c] = (float(v), "--fx")
    return out


# =============================================================================
# ĐỌC THUYẾT MINH VAY: THEO DÒNG (trang native) + THEO CỘT (bảng HTML từ OCR) + VĂN XUÔI
# =============================================================================
MAX_SEG_ITEMS = 200
BADCOL_RE = re.compile(r"lai suat|ky han|dao han|muc dich|tai san|bao dam|the chap|ghi chu|thuyet minh|so hop dong|"
                       r"ma so|kha nang tra no|ty le|%")
LENDER_COL_RE = re.compile(r"ngan hang|ben cho vay|to chuc tin dung|dai ly|chu no|nguoi cho vay|doi tuong")
STT_RE = re.compile(r"^(?:\d{1,2}|[ivx]{1,4}|\(?[a-z]\)?)$", re.I)


def _dedupe_join(parts):
    out = []
    for p in parts:
        if not out or out[-1].lower() != p.lower():
            out.append(p)
    return " ".join(out).strip()


def pick_closing_col(htxt, year):
    """Từ tiêu đề từng cột (đã norm) chọn cột 'Số cuối năm / 31/12/<năm>'.
    Loại cột đầu năm, lãi suất, kỳ hạn, 'số có khả năng trả nợ'... Ưu tiên cột quy đổi VND."""
    cand = []
    for j, h in enumerate(htxt):
        if not h:
            continue
        opening = (f"31/12/{year - 1}" in h or f"01/01/{year}" in h or
                   any(x in h for x in ("dau nam", "dau ky", "nam truoc")))
        cur = (f"31/12/{year}" in h or f"31 thang 12 nam {year}" in h or "cuoi nam" in h or "cuoi ky" in h
               or "nam nay" in h or ("31/12" in h and f"31/12/{year - 1}" not in h))
        if cur and not opening and not BADCOL_RE.search(h):
            cand.append(j)
    if not cand:
        return None
    vnd = [j for j in cand if re.search(r"vnd|quy doi|tuong duong", htxt[j])]
    foreign = [j for j in cand if ccy_from_norm(htxt[j]) and j not in vnd]
    plain = [j for j in cand if j not in vnd and j not in foreign]
    info = {"idx": None, "ccy": None, "vnd_equiv": False, "for_idx": None, "vnd_idx": None}
    if vnd:
        info.update(idx=vnd[0], vnd_equiv=True, vnd_idx=vnd[0])
        if foreign:
            info.update(for_idx=foreign[0], ccy=ccy_from_norm(htxt[foreign[0]]))
    elif plain:
        info.update(idx=plain[0])
    else:
        info.update(idx=foreign[0], ccy=ccy_from_norm(htxt[foreign[0]]))
    return info


class SegState:
    """Trạng thái đọc 1 đoạn thuyết minh vay (dòng text + bảng)."""

    def __init__(self, year, mult, first_heading_raw):
        h0 = classify_heading(norm(first_heading_raw))
        self.cur_type, self.cur_cp = (h0 if h0 else (None, False))
        self.section_state = self.cur_type, self.cur_cp
        self.ccy_ctx = detect_ccy(first_heading_raw)
        self.year, self.mult = year, mult
        self.pending, self.rows = [], []
        self.lender_context = ""
        self.hdr = deque(maxlen=4)
        self.closing_first, self.movement = True, False
        self.prev_col = None
        self.implied = defaultdict(list)
        self.stats = Counter()

    # ---------- dòng text ----------
    def line(self, raw, page):
        n = norm(raw)
        if re.match(r"^\([ivx]+\)\s*(?:khoan vay|trai phieu)\b", n):
            # Explanations belong to narrative_rows, not the table lender buffer.
            self.pending = []
            return
        inline = re.match(r"^(?:Khoản vay\s+\d+|Các khoản vay khác)\s+(\(?[-−–]?\d[\d.,]*\)?)\s*(.*)$", raw, re.I)
        if inline and is_amount(inline.group(1)):
            label = " ".join(filter(None, [self.lender_context, raw[:inline.start(1)].strip()]))
            self.pending = []
            self.entry(label, parse_number(inline.group(1)), page, ctx=inline.group(2), extra_flag="INLINE_LOAN;BANK_DETAIL")
            return
        toks = raw.split()
        k = len(toks)
        while k > 0 and (is_amount(toks[k - 1]) or toks[k - 1] in DASHES):
            k -= 1
        group, label = toks[k:], " ".join(toks[:k]).strip()
        if not any(is_amount(g) for g in group):          # dòng chữ: tiêu đề / header / phần đầu tên bên cho vay
            self.hdr.append(n)
            h = detect_header(" ".join(self.hdr), self.year)
            if h:
                self.closing_first, self.movement = h
            t = classify_heading(n)
            if t and match_bank(raw) is None:
                self.cur_type, self.cur_cp = t
                self.section_state = t
                self.lender_context = ""
                self.ccy_ctx = detect_ccy(raw)
                self.pending = []
                return
            if match_bank(raw) is not None or n.startswith("ngan hang ") or is_generic(n):
                self.lender_context = raw
                self.cur_type, self.cur_cp = self.section_state
            c = detect_ccy(raw)
            if c and len(n.split()) <= 10 and re.search(r"\bvay\b|ngoai te|\bno\b", n):
                self.ccy_ctx = c
            if n and not HEADER_LINE_RE.search(n):
                self.pending = (self.pending + [raw])[-3:]
            return
        vals = [v for v in (parse_number(g) for g in group) if v is not None]
        full = " ".join(self.pending + [label]).strip()
        self.pending = []
        self.entry(full, pick_closing(vals, self.closing_first, self.movement), page)

    # ---------- bảng HTML: đọc theo CỘT ----------
    def table(self, grid, rep, page):
        if not grid:
            return
        k = 0
        while k < min(len(grid), 6) and not any(is_amount(c) for c in grid[k]):
            k += 1
        hdr_rows, data = grid[:k], grid[k:]
        nc = max(len(r) for r in grid)
        htxt = []
        for j in range(nc):
            parts = []
            for r in hdr_rows:
                c = r[j] if j < len(r) else ""
                if c and (not parts or parts[-1] != c):
                    parts.append(c)
            htxt.append(norm(" ".join(parts)))
        for r in hdr_rows:                                 # hàng tiêu đề nhóm nằm trong vùng header
            ne = [c for c in dict.fromkeys(r) if c]
            if len(ne) == 1:
                if match_bank(ne[0]) is not None or norm(ne[0]).startswith("ngan hang ") or is_generic(norm(ne[0])):
                    self.lender_context = ne[0]
                    self.cur_type, self.cur_cp = self.section_state
                t = classify_heading(norm(ne[0]))
                if t:
                    self.cur_type, self.cur_cp = t
                    self.section_state = t
                    self.ccy_ctx = detect_ccy(ne[0])
        info, flag = pick_closing_col(htxt, self.year), ""
        if info:
            self.prev_col = dict(info, nc=nc)
        elif self.prev_col and self.prev_col["nc"] == nc:   # bảng nối trang không lặp header
            info, flag = dict(self.prev_col), "COL_REUSED"
        else:                                               # không có header: đoán theo vị trí cột số
            amt = [j for j in range(nc)
                   if sum(1 for r in data if j < len(r) and is_amount(r[j])) >= max(1, len(data) / 2)
                   and not BADCOL_RE.search(htxt[j])]
            if not amt:
                return
            info = {"idx": amt[pick_closing_pos(len(amt), self.closing_first, self.movement)], "ccy": None,
                    "vnd_equiv": False, "for_idx": None, "vnd_idx": None}
            flag = "COL_FALLBACK"
        self.stats[flag or "COL_OK"] += 1
        idx = info["idx"]
        lender_cols = {j for j, h in enumerate(htxt) if LENDER_COL_RE.search(h)}
        pend = []
        for row in data:
            vcell = row[idx] if idx < len(row) else ""
            lab, ctx = [], []
            for j, c in enumerate(row):
                if not c or is_amount(c) or c in DASHES or STT_RE.match(c):
                    continue
                (lab if (j < idx or j in lender_cols) else ctx).append(c)
            label = _dedupe_join(lab)
            if TOTAL_RE.match(strip_prefix(norm(label))):
                pend = []
                continue
            row_has_amt = any(is_amount(c) for c in row)
            if not label and ctx and not row_has_amt:
                label, ctx = _dedupe_join(ctx), []
            if is_amount(vcell):
                full = " ".join(pend + [label]).strip()
                pend = []
                row_flag = flag
                if re.match(r"^(?:khoan vay\s+\d+|cac khoan vay khac)\b", norm(label)):
                    full = " ".join(filter(None, [self.lender_context, full]))
                    row_flag = ";".join(filter(None, [flag, "BANK_DETAIL"]))
                elif lender_cols and nc <= 3 and any(re.search(r"dau nam|dau ky|nam truoc", h) for h in htxt):
                    if self.lender_context and match_bank(self.lender_context) is None and not norm(full).startswith("ngan hang"):
                        full = self.lender_context + " " + full
                    if match_bank(full) or norm(full).startswith("ngan hang "):
                        row_flag = ";".join(filter(None, [flag, "BANK_SUMMARY"]))
                if info.get("for_idx") is not None and info.get("vnd_idx") is not None and info.get("ccy"):
                    a, b = row[info["for_idx"]], row[info["vnd_idx"]]
                    if is_amount(a) and is_amount(b):
                        fv, vv = parse_number(a), parse_number(b)
                        if fv and vv and fv > 0:
                            self.implied[info["ccy"]].append(vv / fv)
                self.entry(full, parse_number(vcell), page, ctx=" ".join(ctx), col_ccy=info.get("ccy"),
                           vnd_equiv=info.get("vnd_equiv", False), extra_flag=row_flag)
            elif not row_has_amt and label:                # hàng chỉ có chữ: tiêu đề nhóm hoặc dòng đầu tên bên cho vay
                if match_bank(label) is not None or norm(label).startswith("ngan hang ") or is_generic(norm(label)):
                    self.lender_context = label
                    self.cur_type, self.cur_cp = self.section_state
                h = classify_heading(norm(label))
                if h and match_bank(label) is None:
                    self.cur_type, self.cur_cp = h
                    self.section_state = h
                    self.ccy_ctx = detect_ccy(label)
                    self.lender_context = ""
                    pend = []
                else:
                    pend = (pend + [label])[-3:]

    # ---------- xử lý chung 1 dòng/hàng có số ----------
    def entry(self, full_label, v, page, ctx="", col_ccy=None, vnd_equiv=False, extra_flag=""):
        ln = norm(full_label)
        if not ln or v is None:
            return
        sp = strip_prefix(ln)
        tk = match_bank(full_label)
        if TOTAL_RE.match(sp):                            # cộng / tổng / biến động...
            return
        if re.match(r"^phan bo chi phi", sp):
            return
        if tk is None and "thuyet minh" in ln and re.fullmatch(r"vay (?:ngan hang|ben khac)", re.sub(r"\([^)]*\)", "", sp).strip()):
            return  # Summary refers to the detailed loan subnote; do not count both.
        t = classify_heading(ln)
        if tk is None and t:                              # dòng tổng của nhóm (có số) -> chỉ cập nhật nhóm
            self.cur_type, self.cur_cp = t
            if re.sub(r"\([^)]*\)", "", sp).strip() in ("ngan han", "dai han"):
                self.section_state = t
            self.ccy_ctx = detect_ccy(full_label)
            return
        if tk is None and PERIOD_RE.match(sp):            # bảng kỳ hạn thanh toán
            return
        if not v or v < 0:
            return
        row_type = self.cur_type or ST_LOAN
        tk_out = tk if tk else ("UNALLOCATED" if is_generic(ln) else "UNMATCHED")
        low = norm(full_label + " " + ctx)
        if row_type == BOND and tk is not None:
            direct = bool(BUY_RE.search(low)) and not AGENT_RE.search(low)
        else:
            direct = True
        flags = [f for f in ("CURRENT_PORTION_LT" if self.cur_cp else "", extra_flag) if f]
        ccy = col_ccy or detect_ccy(full_label) or self.ccy_ctx
        fx_pending, ccy_out = False, "VND"
        if ccy and vnd_equiv:
            flags.append("FX_VND_COLUMN")
        elif ccy:
            if ccy in ("USD", "EUR") and v * self.mult >= 1e9:   # số quá lớn để là nguyên tệ -> coi là đã quy đổi VND
                flags.append("FX_ASSUMED_VND")
            else:
                fx_pending, ccy_out = True, ccy
        self.rows.append({
            "Loan_Type": row_type, "Bank_Name_Raw": re.sub(r"\s+", " ", full_label).strip(),
            "Bank_Ticker_Matched": tk_out, "amount_raw": v, "Is_Direct_Loan": direct,
            "Source_Page": page + 1, "Note_Flag": ";".join(flags),
            "ccy": ccy_out, "fx_pending": fx_pending, "abs_vnd": False,
        })


def iter_items(pages, start):
    """Duyệt các trang từ start thành chuỗi ('L', trang, dòng) và ('T', trang, ('table', grid, rep))."""
    for pi in range(start, len(pages)):
        p = pages[pi]
        blocks = getattr(p, "blocks", None)
        if blocks:
            for b in blocks:
                if b[0] == "text":
                    for l in b[1]:
                        yield ("L", pi, l)
                else:
                    yield ("T", pi, b)
        else:
            for l in p:
                yield ("L", pi, l)


def find_loan_segments(pages, start_page):
    flat = list(iter_items(pages, start_page))
    segs, i = [], 0
    while i < len(flat):
        kind, pi, ln = flat[i]
        if kind == "L":
            n = norm(ln)
            m = HEAD_RE.match(n)
            if m and not has_amount(ln) and len(n) < 120:
                no = int(m.group(1)) if m.group(1) else None
                seg, j = [flat[i]], i + 1
                while j < len(flat) and j - i < MAX_SEG_ITEMS:
                    kj, pj, lj = flat[j]
                    if kj == "L":
                        nj = norm(lj)
                        if is_next_note_heading(nj, lj, no) or (HEAD_RE.match(nj) and not has_amount(lj)):
                            break
                    seg.append(flat[j])
                    j += 1
                segs.append(seg)
                i = j
                continue
        i += 1
    return segs


def process_segment(seg, year, mult):
    st = SegState(year, mult, seg[0][2])
    for kind, pi, payload in seg[1:]:
        if kind == "L":
            st.line(payload, pi)
        else:
            st.table(payload[1], payload[2], pi)
    return st.rows, st.implied, st.stats


# =============================================================================
# THUYẾT MINH DẠNG VĂN XUÔI (Narrative Parser)
# =============================================================================
BAL_RE = re.compile(r"du no|so du (?:vay|khoan vay|no)|con no|tong no vay|so tien vay con lai")
NARR_EXCL_RE = re.compile(r"the chap|cam co|tai san bao dam|bao lanh")
DEPOSIT_RE = re.compile(r"tien gui|gui tai|chung chi tien gui|tai khoan thanh toan|tai khoan ngan hang")
LOAN_KW_RE = re.compile(r"\bvay\b|tin dung|trai phieu|han muc|thau chi")
AMT_NARR_RE = re.compile(
    r"(?<![\d/.,])(?P<cur1>usd|eur|jpy)?\s*(?P<num>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"\s*(?P<scale>ty|trieu|nghin)?\b(?!\s*le\b)(?:\s*(?P<cur2>dong|vnd|usd|eur|jpy|do la my))?(?![\d/%])")


def find_amounts(n):
    """Các số tiền trong câu (n đã norm). Số có đơn vị (tỷ/triệu/VND) là tuyệt đối; số trần phải có dấu phân cách nghìn."""
    out = []
    for m in AMT_NARR_RE.finditer(n):
        num = m.group("num")
        scale, cur2, cur1 = m.group("scale"), m.group("cur2"), m.group("cur1")
        explicit = bool(scale or cur2 or cur1)
        v = parse_scaled(num)
        if v is None:
            continue
        if not explicit and (not re.search(r"[.,]\d{3}", num) or v < 1e5):
            continue
        cc = (cur2 or cur1 or "")
        ccy = {"do la my": "USD"}.get(cc, cc.upper()) if cc and cc != "dong" else None
        if ccy == "VND":
            ccy = None
        out.append({"pos": m.start(), "value": v * SCALE.get(scale, 1.0), "ccy": ccy,
                    "abs_vnd": bool(scale or cur2 == "dong" or cur2 == "vnd")})
    return out


def match_all_banks(raw):
    """Mọi ngân hàng trong BANK_MAP xuất hiện trong text (đã loại chồng lấn: 'Đông Nam Á' không kéo theo 'Nam Á')."""
    if not raw:
        return []
    n = norm(raw)
    has_bank_word = "ngan hang" in n or "bank" in n
    ctx = bool(re.search(r"ngan hang|bank|chi nhanh|\bcn\b|\bnh\b|tmcp", n))
    found = []
    for a, tk, kind, rx in ALIASES:
        if kind == "ticker":
            if ctx and rx.search(raw) and tk not in found:
                found.append(tk)
            continue
        if kind == "short_vn" and not has_bank_word:
            continue
        m = rx.search(n)
        if m:
            if tk not in found:
                found.append(tk)
            n = n[:m.start()] + " " * (m.end() - m.start()) + n[m.end():]
    return found


def _narrative_chunks(pages, start):
    """Gom các dòng text liên tiếp (qua nhiều trang) thành đoạn; bảng và gạch đầu dòng ngắt đoạn."""
    chunks, buf, pg = [], [], None

    def flush():
        nonlocal buf, pg
        if buf:
            chunks.append((pg, " ".join(buf)))
        buf, pg = [], None

    for kind, pi, payload in iter_items(pages, start):
        if kind == "T":
            flush()
            continue
        line = payload.strip()
        if not line:
            continue
        if re.match(r"^(?:[-–•+]\s|\(?[a-z0-9ivx]{1,3}[\)\.]\s)", line, re.I):
            flush()
        if pg is None:
            pg = pi
        buf.append(line)
    flush()
    return chunks


def _split_sentences(text):
    text = re.sub(r"\b(TP|CN|Q|P|TX|TT|ĐT)\.\s+", r"\1 ", text)
    return [x.strip() for x in re.split(r"(?<=[\.;])\s+(?=[A-ZÀ-ỸĐ(])", text) if x.strip()]


def narrative_rows(pages, start, year, default_type):
    """Bắt các khoản vay viết dạng câu: 'Khoản vay dài hạn tại Ngân hàng X ... dư nợ tại ngày 31/12/N là Y tỷ đồng'."""
    out = []
    for page, chunk in _narrative_chunks(pages, start):
        sents = _split_sentences(chunk)
        i = 0
        while i < len(sents):
            n0 = norm(sents[i])
            if not LOAN_KW_RE.search(n0) or (not match_all_banks(sents[i]) and not is_generic(n0)):
                i += 1
                continue
            unit, j = sents[i], i
            while not (BAL_RE.search(norm(unit)) and find_amounts(norm(unit))) and j < min(len(sents) - 1, i + 2):
                if match_all_banks(sents[j + 1]):
                    break
                j += 1
                unit += " " + sents[j]
            row = _narrative_one(unit, page, year, default_type)
            if row:
                out.append(row)
                i = j + 1
            else:
                i += 1
    return out


def _narrative_one(unit, page, year, default_type):
    n = norm(unit)
    if DEPOSIT_RE.search(n) or (NARR_EXCL_RE.search(n) and "du no" not in n):
        return None
    mb = BAL_RE.search(n)
    amts = find_amounts(n)
    if not mb or not amts:
        return None
    cur_pos = max(n.find(f"31/12/{year}"), n.find(f"31 thang 12 nam {year}"))
    prior_pos = max(n.find(f"31/12/{year - 1}"), n.find(f"31 thang 12 nam {year - 1}"))
    cands = [a for a in amts if a["pos"] >= mb.start()]
    if cur_pos >= 0 and any(a["pos"] >= cur_pos for a in amts):
        cands = [a for a in amts if a["pos"] >= cur_pos]
    if not cands:
        return None
    a = cands[0]
    if prior_pos >= 0 and cur_pos < 0 and prior_pos < a["pos"]:      # số liệu của năm trước
        return None
    banks = match_all_banks(unit)
    flags = ["NARRATIVE"]
    if len(banks) == 1:
        tk = banks[0]
    elif len(banks) > 1:
        tk = "UNALLOCATED"
        flags.append("NARRATIVE_MULTI_BANK")
    elif is_generic(n):
        tk = "UNALLOCATED"
    else:
        return None
    m = re.search(r"(?:Ngân hàng|NH)[^,;:()\d]{3,100}", unit, re.I)
    raw_name = (m.group(0) if m else unit[:120]).strip()
    if len(banks) > 1:
        raw_name = unit[:200]
    has_st, has_lt = bool(re.search(r"\bngan han\b", n)), bool(re.search(r"\bdai han\b", n))
    if "trai phieu" in n:
        typ = BOND
    elif has_lt and not has_st:
        typ = LT_LOAN
    elif has_st and not has_lt:
        typ = ST_LOAN
    else:
        typ = default_type or ST_LOAN
    direct = not (typ == BOND and AGENT_RE.search(n))
    return {"Loan_Type": typ, "Bank_Name_Raw": re.sub(r"\s+", " ", raw_name), "Bank_Ticker_Matched": tk,
            "amount_raw": a["value"], "Is_Direct_Loan": direct, "Source_Page": page + 1,
            "Note_Flag": ";".join(flags), "ccy": a["ccy"] or "VND", "fx_pending": bool(a["ccy"]),
            "abs_vnd": (not a["ccy"]) and a["abs_vnd"]}


# =============================================================================
# PHÂN TÍCH 1 FILE
# =============================================================================
def finalize_row(r, mult, fx):
    """Chuẩn hoá 1 dòng vay: quy đổi về VND (đơn vị BCTC / tỷ giá nếu là ngoại tệ)."""
    flags = [f for f in r.get("Note_Flag", "").split(";") if f]
    cur, orig, rate = r.get("ccy") or "VND", None, None
    if r.get("fx_pending"):
        orig = r["amount_raw"]
        hit = fx.get(cur)
        if hit:
            rate = hit[0]
            vnd = int(round(orig * rate))
            flags.append(f"FX_CONVERTED({hit[1]})")
        else:
            vnd = None
            flags.append("FX_NO_RATE")
    elif r.get("abs_vnd"):
        vnd = int(round(r["amount_raw"]))
    else:
        vnd = int(round(r["amount_raw"] * mult))
    return {"Loan_Type": r["Loan_Type"], "Bank_Name_Raw": r["Bank_Name_Raw"],
            "Bank_Ticker_Matched": r["Bank_Ticker_Matched"], "Loan_Amount_VND": vnd,
            "Is_Direct_Loan": r["Is_Direct_Loan"], "Currency": cur, "Original_Amount": orig,
            "FX_Rate": rate, "Source_Page": r["Source_Page"], "Note_Flag": ";".join(flags)}


def analyze(pages, year, fx_cfg=None):
    res = {"fin": {}, "rows": [], "notes": [], "unit_label": "", "found": False, "stats": Counter(), "fx": {}}
    bs_i = find_stmt_start(pages, "bang can doi ke toan")
    is_i = find_stmt_start(pages, "ket qua hoat dong kinh doanh", from_idx=bs_i or 0)
    mult, unit_label = detect_unit(pages, [bs_i, is_i])
    res["unit_label"] = unit_label
    if unit_label.startswith("KHÔNG"):
        res["notes"].append("Không thấy dòng 'Đơn vị tính' -> giả định VND")
    head = norm(" ".join(l for p in pages[:25] for l in p[:60]))
    if re.search(r"don vi tinh\s*[:\-]?\s*\(?(usd|eur)\b", head):
        res["foreign_report"] = True
        res["notes"].append("BCTC lập bằng ngoại tệ (USD/EUR) -> số liệu chưa phải VND, cần quy đổi tay")

    fin = {}
    if bs_i is not None:
        span = is_i - bs_i if is_i is not None and is_i > bs_i else 6
        fin.update(extract_codes(pages, bs_i, span, ["270", "300", "320", "338", "400"], year))
        if not re.search(rf"31\s*/\s*12\s*/\s*{year}|31 thang 12 nam {year}",
                         norm(" ".join(" ".join(p) for p in pages[bs_i:bs_i + 2]))):
            res["notes"].append(f"Không thấy mốc 31/12/{year} ở trang CĐKT (kiểm tra tên file/năm)")
    else:
        res["notes"].append("Không tìm thấy trang CĐKT")
    if is_i is not None:
        fin.update(extract_codes(pages, is_i, 2, ["50", "23"], year))
    else:
        res["notes"].append("Không tìm thấy trang BCKQKD")
    res["fin"] = {k: int(round(v * mult)) for k, v in fin.items()}
    f = res["fin"]
    if all(k in f for k in ("270", "300", "400")) and f["270"]:
        if abs(f["270"] - f["300"] - f["400"]) / abs(f["270"]) > BALANCE_TOL:
            res["notes"].append("CĐKT không cân: 270 != 300 + 400")
    res["found"] = all(k in f for k in ("270", "300", "400", "50"))

    start = (is_i + 1) if is_i is not None else ((bs_i + 2) if bs_i is not None else 0)
    raw_rows, seen, implied = [], set(), defaultdict(list)
    segs = find_loan_segments(pages, start)
    res["n_segments"] = len(segs)
    for seg in segs:
        rows, imp, st = process_segment(seg, year, mult)
        res["stats"].update(st)
        for c, lst in imp.items():
            implied[c].extend(lst)
        for r in rows:
            key = (r["Loan_Type"], norm(r["Bank_Name_Raw"]), r["amount_raw"], r.get("ccy"), r["Note_Flag"])
            if key in seen:
                continue
            seen.add(key)
            raw_rows.append(r)
    def bank_group(r):
        lender = r["Bank_Ticker_Matched"]
        if lender in ("UNMATCHED", "UNALLOCATED"):
            lender = re.split(r"\bkhoan vay\s+\d+\b|\bcac khoan vay khac\b", norm(r["Bank_Name_Raw"]))[0].strip()
        return lender, r["Loan_Type"], r.get("ccy") or "VND"
    bank_summaries = {bank_group(r) for r in raw_rows if "BANK_SUMMARY" in _flags(r)}
    detail_count = sum("BANK_DETAIL" in _flags(r) and bank_group(r) in bank_summaries for r in raw_rows)
    if detail_count:
        res["notes"].append(f"Dùng bảng tổng hợp theo ngân hàng; bỏ {detail_count} dòng chi tiết đã được tổng hợp để tránh cộng trùng")
        raw_rows = [r for r in raw_rows if not ("BANK_DETAIL" in _flags(r) and bank_group(r) in bank_summaries)]
    fx = resolve_fx(pages, year, implied, fx_cfg)
    res["fx"] = fx
    final = [finalize_row(r, mult, fx) for r in raw_rows]

    # Văn xuôi: chỉ thêm khoản chưa có (trùng số tiền với dòng bảng thì bỏ)
    def loan_key(r):
        lender = r["Bank_Ticker_Matched"]
        if lender in ("UNMATCHED", "UNALLOCATED"):
            lender = norm(r["Bank_Name_Raw"])
        return lender, r["Loan_Type"], r["Currency"], r["Loan_Amount_VND"]
    have = {loan_key(r) for r in final if r["Loan_Amount_VND"]}
    n_narr = 0
    for r in narrative_rows(pages, start, year, None):
        if bank_group(r) in bank_summaries:
            continue
        fr = finalize_row(r, mult, fx)
        v = fr["Loan_Amount_VND"]
        if v and loan_key(fr) in have:
            continue
        if v:
            have.add(loan_key(fr))
        final.append(fr)
        n_narr += 1
    res["stats"]["NARRATIVE_ROWS"] = n_narr
    res["rows"] = final
    return res


def dump_debug(cfg, fname, pages):
    d = (cfg or {}).get("debug_dir")
    if not d:
        return
    try:
        Path(d).mkdir(parents=True, exist_ok=True)
        with open(Path(d) / (Path(fname).stem + ".txt"), "w", encoding="utf-8") as fh:
            for i, p in enumerate(pages, 1):
                fh.write(f"\n===== TRANG {i} =====\n" + "\n".join(p) + "\n")
    except Exception:
        pass


def _text_source(info, engine):
    if info["ocr_pages"] == 0:
        return "NATIVE"
    return "OCR" if engine == "ocr" else "MIXED"


def _flags(r):
    return set(f.split("(")[0] for f in r["Note_Flag"].split(";") if f)


def process_file(path, cfg=None):
    fname = os.path.basename(path)
    log = {"File_Name": fname, "Status_Flag": CHECK, "Sum_Extracted_Loans": None,
           "Total_Debt_BS": None, "Text_Source": "", "Notes": ""}
    m = FILE_RE.match(fname)
    if not m:
        log["Notes"] = "Tên file không đúng quy cách [Mã]_[Năm].pdf"
        return {"loans": [], "log": log}
    ticker, year = m.group(1).upper(), int(m.group(2))
    fx_cfg = (cfg or {}).get("fx")

    try:
        pages, engine, info = load_pages(path, cfg, force_ocr=bool(cfg and cfg.get("force")))
    except Exception as e:  # file hỏng / lỗi OCR
        log["Notes"] = f"Lỗi đọc PDF: {e}"
        return {"loans": [], "log": log}
    if not pages:
        log["Status_Flag"] = SCANNED
        ocr_on = bool(cfg and cfg.get("enabled"))
        why = (f"OCR thất bại {info['ocr_failed']} trang. Lỗi: {' | '.join(info.get('errors', []))}"
               if ocr_on and info["ocr_failed"] else
               ("OCR đã bật nhưng không lấy được chữ nào" if ocr_on else
                "CHƯA CÓ BACKEND OCR (chưa chạy server dots.mocr / OCR bị tắt)"))
        log["Notes"] = "Không đọc được text (PDF scan/ảnh); " + why
        return {"loans": [], "log": log}

    dump_debug(cfg, fname, pages)
    res = analyze(pages, year, fx_cfg)
    # Không bắt được số liệu -> thử pdfplumber (text-layer), rồi OCR toàn bộ file (font lỗi/text-layer rác)
    if not res["found"] and engine.startswith("fitz") and pdfplumber is not None and info["ocr_pages"] == 0:
        try:
            alt = analyze(read_pdfplumber(path), year, fx_cfg)
            if len(alt["fin"]) > len(res["fin"]):
                res, engine = alt, "pdfplumber"
        except Exception:
            pass
    if not res["found"] and cfg and cfg.get("enabled") and engine != "ocr":
        try:
            pages2, engine2, info2 = load_pages(path, cfg, force_ocr=True)
            if pages2:
                dump_debug(cfg, fname, pages2)
                res2 = analyze(pages2, year, fx_cfg)
                if len(res2["fin"]) >= len(res["fin"]):
                    res, engine, info = res2, engine2, info2
        except Exception:
            pass

    f = res["fin"]
    log["Text_Source"] = _text_source(info, engine)
    notes = [f"engine={engine}", f"unit={res['unit_label']}"] + res["notes"]
    if info["ocr_pages"]:
        bk = ", ".join(f"{k}:{v}" for k, v in info.get("backends", {}).items())
        notes.append(f"OCR {info['ocr_pages']} trang [{bk}] (nên đối chiếu tay số liệu quan trọng)")
    if info.get("rotated"):
        notes.append(f"{info['rotated']} trang được tự xoay trước khi OCR")
    if info["ocr_failed"]:
        notes.append(f"OCR lỗi {info['ocr_failed']} trang: {' | '.join(info.get('errors', []))}")
    if info.get("invalid_json"):
        notes.append("Cache OCR JSON không hợp lệ ở trang " + ",".join(map(str, info["invalid_json"])) +
                     "; số liệu có thể thiếu/sai, chọn chạy lại khi server hoạt động")
    st = res["stats"]
    if st.get("COL_FALLBACK"):
        notes.append(f"{st['COL_FALLBACK']} bảng không có header cột -> đoán cột theo vị trí (kiểm tra)")
    if st.get("NARRATIVE_ROWS"):
        notes.append(f"{st['NARRATIVE_ROWS']} khoản lấy từ văn xuôi (cờ NARRATIVE)")
    if res["fx"]:
        notes.append("tỷ giá: " + ", ".join(f"{c}={v[0]:,.0f}({v[1]})" for c, v in res["fx"].items()))

    # Tổng nợ vay trên CĐKT = mã 320 + mã 338 (chỉ dùng để đối chiếu, không xuất sheet riêng)
    parts = [f.get("320"), f.get("338")]
    total_debt = None if any(x is None for x in parts) else sum(parts)
    if f.get("320") is None or f.get("338") is None:
        notes.append("Thiếu mã 320 hoặc 338 trên CĐKT")
    log["Total_Debt_BS"] = total_debt

    rows = res["rows"]
    loans = [dict(Corporate_Ticker=ticker, Year=year, **r) for r in rows]
    n_nofx = sum(1 for r in rows if r["Loan_Amount_VND"] is None)
    if n_nofx:
        notes.append(f"{n_nofx} khoản ngoại tệ CHƯA có tỷ giá (FX_NO_RATE) -> điền --fx hoặc --fx-file")

    # Reconcile exactly the rows exported, without selecting a subset to fit the target.
    valid = [r for r in rows if r["Loan_Amount_VND"] is not None]
    chosen = sum(r["Loan_Amount_VND"] for r in valid)
    log["Sum_Extracted_Loans"] = chosen
    unmatched = sum(r["Loan_Amount_VND"] for r in valid if r["Bank_Ticker_Matched"] == "UNMATCHED")
    share_unmatched = unmatched / chosen if chosen else 0

    # ---- gán cờ trạng thái ----
    if res.get("foreign_report"):
        status = CHECK
    elif total_debt is None:
        status = CHECK
        notes.append("Không đọc được mã 320/338 trên CĐKT (không đối chiếu được)")
    elif total_debt == 0 and not rows:
        status = OK
        notes.append("Không có vay nợ trên CĐKT")
    elif not rows:
        status = CHECK
        notes.append("Không bóc được dòng vay nào (không tìm thấy thuyết minh / trình bày lạ)")
    elif total_debt == 0 or abs(chosen - total_debt) / max(abs(total_debt), 1) > MISMATCH_TOL:
        status = MISMATCH
        notes.append(f"Lệch {abs(chosen - total_debt):,.0f} VND so với CĐKT")
    elif n_nofx or share_unmatched > UNMATCHED_SHARE_WARN:
        status = CHECK
        if share_unmatched > UNMATCHED_SHARE_WARN:
            notes.append(f"{share_unmatched:.0%} dư nợ thuộc bên cho vay không có trong từ điển")
    else:
        status = OK
    if status == OK and (info.get("invalid_json") or info["ocr_failed"]):
        status = CHECK
    log["Status_Flag"] = status
    log["Notes"] = "; ".join(notes)
    return {"loans": loans, "log": log}


def check_ocr_server(url, timeout=10):
    if requests is None:
        return False, "thiếu thư viện requests (pip install requests)"
    try:
        r = requests.get(url.rstrip("/") + "/models", timeout=timeout)
        r.raise_for_status()
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def build_backends(args):
    """Direct CUDA by default; legacy vLLM only when explicitly selected."""
    if getattr(args, "ocr_backend", "vllm") == "direct":
        return [{"name": "dots.mocr GPU", "kind": "direct", "model_path": args.model_path,
                 "prompt": OCR_PROMPT, "max_tokens": args.ocr_max_tokens, "retries": 1}], \
               [f"OCR dots.mocr: GPU trực tiếp, không dùng server ({args.model_path})"]
    ok, err = check_ocr_server(args.ocr_url)
    if not ok:
        return [], [f"[!] Server dots.mocr không phản hồi tại {args.ocr_url} ({err})"]
    backend = {"name": "dots.mocr", "kind": "openai", "url": args.ocr_url,
               "model": args.ocr_model, "prefix": not args.ocr_no_img_prefix,
               "prompt": OCR_PROMPT, "key": None, "max_tokens": args.ocr_max_tokens,
               "retries": 3, "timeout": 300}
    return [backend], [f"OCR local dots.mocr: BẬT ({args.ocr_url}, model='{args.ocr_model}')"]


def build_fx_cfg(args):
    cfg = {"override": {}, "file": {}}
    for part in filter(None, (args.fx or "").split(",")):
        try:
            c, v = part.split("=")
            cfg["override"][c.strip().upper()] = float(v.replace(".", "").replace(",", "")) if v.count(".") > 1 else float(v)
        except ValueError:
            print(f"[!] Bỏ qua --fx không hợp lệ: {part}")
    if args.fx_file:
        try:
            cfg["file"] = {k.upper(): v for k, v in json.loads(Path(args.fx_file).read_text(encoding="utf-8")).items()}
        except Exception as e:  # noqa: BLE001
            print(f"[!] Không đọc được --fx-file: {e}")
    return cfg


def run_test_ocr(args, files):
    """Gọi thử dots.mocr trên 1 trang để kiểm tra model / server."""
    if fitz is None:
        sys.exit("Cần cài pymupdf: pip install pymupdf")
    bes, msgs = build_backends(args)
    for m_ in msgs:
        print(m_)
    if not bes:
        sys.exit("\n=> KHÔNG có backend OCR nào. Hãy bật server dots.mocr tại --ocr-url.")
    if not files:
        sys.exit("Không có PDF để thử")
    path = files[0]
    with fitz.open(path) as doc:
        page = doc[min(len(doc) - 1, 5)]
        page_number = page.number + 1
        png = render_png(page, 120, 0)
    for be in bes:
        be = dict(be, retries=1, timeout=120)
        print(f"\nĐang OCR 1 trang ({Path(path).name}, trang {page_number}) bằng {be['name']}...", flush=True)
        try:
            t0 = time.time()
            raw = _call_backend(png, be)
            pg = ocr_response_to_page(raw)
            print(f"[OK] {be['name']}: {len(pg)} dòng, {time.time() - t0:.1f}s. Mẫu:")
            for l in list(pg)[:5]:
                print("   ", l[:110])
        except Exception as e:  # noqa: BLE001
            sys.exit(f"[LỖI] {be['name']}: {e}")


def main():
    ap = argparse.ArgumentParser(description="Trích xuất BCTC kiểm toán hợp nhất từ PDF (hỗ trợ OCR dots.mocr)")
    ap.add_argument("--pdf-dir", default=str(Path(__file__).resolve().parent / "Source"))
    ap.add_argument("--output-dir", "--output", dest="output_dir", default=str(Path(__file__).resolve().parent / "result" / "data"),
                    help="thư mục lưu CSV và SQLite (không phải đường dẫn .xlsx)")
    ap.add_argument("--rerun", action="store_true", help="chạy lại file đã có kết quả trong SQLite; mặc định bỏ qua")
    ap.add_argument("--workers", type=int, default=1, help="số file xử lý song song")
    ap.add_argument("--no-ocr", action="store_true", help="tắt OCR (file scan sẽ bị gán [SCANNED_PDF])")
    ap.add_argument("--force-ocr", action="store_true", help="OCR toàn bộ trang của mọi file (kể cả file có text-layer)")
    ap.add_argument("--ocr-backend", choices=["direct", "vllm"], default="direct", help="mặc định nạp model trực tiếp trên GPU")
    ap.add_argument("--model-path", default=str(Path(__file__).resolve().parent / "weights" / "DotsMOCR"))
    ap.add_argument("--ocr-url", default="http://localhost:8000/v1", help="endpoint OpenAI-compatible của vLLM (dots.mocr)")
    ap.add_argument("--ocr-model", default="model", help="tên model (--served-model-name của vLLM)")
    ap.add_argument("--ocr-threads", type=int, default=1, help="số request OCR đồng thời cho mỗi file")
    ap.add_argument("--ocr-dpi", type=int, default=200)
    ap.add_argument("--ocr-cache", default="./ocr_cache_dots_mocr")
    ap.add_argument("--ocr-max-tokens", type=int, default=16000)
    ap.add_argument("--ocr-no-img-prefix", action="store_true",
                    help="không thêm '<|img|><|imgpad|><|endofimg|>' vào prompt (thử nếu kết quả OCR rỗng/lạ)")
    ap.add_argument("--auto-rotate", choices=["score", "landscape", "off"], default="score",
                    help="score: OCR bình thường, nếu kết quả rác thì thử xoay 90/270/180 (mặc định, an toàn cho trang ngang "
                         "có chữ đứng); landscape: luôn xoay trang ngang thành trang đứng trước khi OCR; off: không xoay")
    ap.add_argument("--fx", default="", help="tỷ giá tay, vd USD=24260,EUR=26500 (ưu tiên cao nhất)")
    ap.add_argument("--fx-file", default="", help='JSON tỷ giá theo năm, vd {"USD":{"2021":22800,"2022":23500}}')
    ap.add_argument("--test-ocr", action="store_true", help="chỉ kiểm tra dots.mocr (server/model) trên 1 trang rồi thoát")
    ap.add_argument("--only", default="", help="chỉ chạy các file có tên chứa 1 trong các chuỗi này (phẩy ngăn cách), vd: VIC_2021,NVL_2023")
    ap.add_argument("--files", nargs="+", help="đường dẫn chính xác các PDF cần chạy, nằm trong --pdf-dir")
    ap.add_argument("--limit", type=int, default=0, help="chỉ chạy N file đầu tiên (chạy thử)")
    ap.add_argument("--debug-dir", default="./debug_text", help="thư mục ghi text đã đọc của từng file ('' để tắt)")
    args = ap.parse_args()
    if args.workers < 1 or args.ocr_threads < 1:
        ap.error("--workers và --ocr-threads phải lớn hơn 0")
    if not args.no_ocr and args.ocr_backend == "direct" and (args.workers != 1 or args.ocr_threads != 1):
        ap.error("GPU trực tiếp dùng --workers 1 --ocr-threads 1 để chỉ nạp một model")
    if Path(args.output_dir).suffix.lower() in (".xlsx", ".csv"):
        ap.error("--output-dir cần một thư mục; chương trình tạo hai CSV và extraction.sqlite3 trong đó")

    if fitz is None and pdfplumber is None:
        sys.exit("Cần cài ít nhất một trong: pymupdf, pdfplumber")
    files = sorted(str(p) for p in Path(args.pdf_dir).rglob("*") if p.suffix.lower() == ".pdf")
    if args.files:
        selected = {Path(p).resolve() for p in args.files}
        available = {Path(p).resolve() for p in files}
        missing = selected - available
        if missing:
            ap.error("PDF không tồn tại trong --pdf-dir: " + ", ".join(str(p) for p in sorted(missing)))
        files = [p for p in files if Path(p).resolve() in selected]
    if args.only:
        keys = [k.strip().lower() for k in args.only.split(",") if k.strip()]
        files = [f for f in files if any(k in os.path.basename(f).lower() for k in keys)]
    if args.limit:
        files = files[:args.limit]
    if not files:
        sys.exit(f"Không tìm thấy file PDF phù hợp trong {args.pdf_dir}")
    if args.test_ocr:
        run_test_ocr(args, files)
        return

    with ResultStore(args.output_dir) as store:
        selected_total = len(files)
        skipped = 0
        if not args.rerun:
            pending = []
            for path in files:
                if store.has_file(path):
                    skipped += 1
                    print(f"Bỏ qua (đã có kết quả): {Path(path).name}", flush=True)
                else:
                    pending.append(path)
            files = pending
        print(f"Đã có {skipped}/{selected_total} file; cần xử lý {len(files)} file.", flush=True)
        cfg = {"enabled": False, "debug_dir": args.debug_dir, "fx": build_fx_cfg(args)}
        if files and not args.no_ocr:
            if fitz is None:
                sys.exit("Cần PyMuPDF để render trang thành ảnh cho dots.mocr: pip install pymupdf")
            else:
                backends, msgs = build_backends(args)
                for m_ in msgs:
                    print(m_)
                has_cache = Path(args.ocr_cache).exists() and any(Path(args.ocr_cache).rglob("p*.json"))
                if backends or has_cache:
                    cfg.update(enabled=True, backends=backends, threads=args.ocr_threads, dpi=args.ocr_dpi,
                               cache_dir=args.ocr_cache, force=args.force_ocr, rotate=args.auto_rotate)
                    if not backends:
                        print(f"[!] Không có backend OCR nào chạy được -> chỉ dùng cache tại {args.ocr_cache}; "
                              f"trang chưa cache sẽ báo lỗi.")
                else:
                    sys.exit("Không có server dots.mocr hoặc cache OCR. Hãy bật vLLM tại --ocr-url; "
                             "dùng --no-ocr nếu chỉ muốn đọc text-layer.")
        print(f"Tìm thấy {len(files)} file PDF. Đang xử lý với {args.workers} tiến trình...")

        if files and cfg.get("backends") and cfg["backends"][0].get("kind") == "direct":
            from dots_mocr_gpu import prepare
            try:
                prepare(cfg["backends"][0])
            except Exception as error:
                sys.exit(f"Không nạp được dots.mocr trên GPU: {error}. Chạy bash run_gpu.sh")

        print(f"CSV đã tạo tại: {store.output_dir.resolve()} (cập nhật sau mỗi PDF)", flush=True)
        if args.workers == 1:
            for i, p in enumerate(files, 1):
                print(f"\nĐã xử lý/bỏ qua {i - 1 + skipped}/{selected_total} file. Đang chạy: {os.path.basename(p)}", flush=True)
                t0 = time.time()
                result = process_file(p, cfg)
                store.save_file(p, result)
                print(f"[{i + skipped}/{selected_total}] {os.path.basename(p)} -> {result['log']['Status_Flag']} "
                      f"({result['log']['Text_Source']}, {time.time() - t0:.0f}s) — đã lưu SQLite + CSV", flush=True)
                del result
        else:
            paths = iter(files)
            completed = skipped
            with ProcessPoolExecutor(max_workers=args.workers) as ex:
                futures = {ex.submit(process_file, p, cfg): p for p in islice(paths, args.workers)}
                while futures:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        p = futures.pop(future)
                        try:
                            result = future.result()
                        except Exception as e:
                            result = {"loans": [], "log": {"File_Name": os.path.basename(p),
                                      "Status_Flag": CHECK, "Sum_Extracted_Loans": None, "Total_Debt_BS": None,
                                      "Text_Source": "", "Notes": f"Lỗi không xác định: {e}"}}
                        store.save_file(p, result)
                        completed += 1
                        print(f"[{completed}/{selected_total}] {os.path.basename(p)} -> "
                              f"{result['log']['Status_Flag']} — đã lưu SQLite + CSV", flush=True)
                        del result
                        next_path = next(paths, None)
                        if next_path is not None:
                            futures[ex.submit(process_file, next_path, cfg)] = next_path

        print("\n=== TỔNG KẾT ===")
        for status, count in store.summary().items():
            print(f"{status}: {count}")
        got = set()
        for name in store.file_names():
            match = FILE_RE.match(name)
            if match:
                got.add((match.group(1).upper(), int(match.group(2))))
    if not args.only and not args.limit and not args.files:      # kiểm tra đủ bộ ticker x năm 2021-2025
        tick = sorted({t for t, _ in got})
        miss = [f"{t}_{y}" for t in tick for y in range(2021, 2026) if (t, y) not in got]
        print(f"\nSố DN: {len(tick)} | Số file: {len(got)} | Thiếu so với 5 năm/DN: {miss if miss else 'không'}")
    print(f"\nĐã lưu CSV và SQLite: {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
