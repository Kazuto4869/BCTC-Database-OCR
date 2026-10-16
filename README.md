# Trích xuất BCTC bằng dots.mocr trên GPU

Chạy model trực tiếp bằng Transformers trên GPU NVIDIA. Mặc định không dùng
server vLLM, Gemini hay API trả phí. Model dùng token để sinh kết quả; token
không phải phí API khi chạy trọng số trên máy.

## Chạy trên máy này

```bash
cd "/home/thanh-dat/Documents/Project/Extract BTCT"
bash run_gpu.sh
```

Hoặc `python main.py`: menu tự chọn Python tại `.venv-dots-gpu/bin/python`
nếu môi trường này tồn tại. Không cần bật `run_server.sh`.

Mặc định menu dùng cache OCR hợp lệ, rồi chữ có sẵn trong PDF; chỉ gọi GPU cho
trang ít chữ/scan còn thiếu. Dùng `bash run_gpu.sh --force-ocr` để ép OCR mọi trang.

Menu chọn tất cả PDF hoặc số file như `1,3,5-8`, rồi chọn:

- Chế độ 1: bỏ qua file đã lưu; tự thử lại file lỗi đọc/OCR hoặc cache JSON hỏng.
- Chế độ 2: phân tích lại mọi file đã chọn, dùng lại các trang OCR cache hợp lệ.

Sau khi sửa parser, chọn chế độ 2 một lần để cập nhật kết quả cũ.
Không chạy hai batch cùng lúc trên một GPU hoặc cùng thư mục kết quả.

Kiểm tra hoặc chạy bằng CLI:

```bash
.venv-dots-gpu/bin/python thesis.py --test-ocr --only BCM_2021
.venv-dots-gpu/bin/python thesis.py --force-ocr --workers 1 --ocr-threads 1
.venv-dots-gpu/bin/python thesis.py --force-ocr --rerun --only BCM_2025
```

`--no-ocr` chỉ đọc text có sẵn. Không có `--force-ocr`, chỉ trang ít chữ mới OCR.
GPU trực tiếp bắt buộc một worker và một luồng OCR, nạp model một lần.

## Môi trường và trọng số

`.venv-dots-gpu` dùng Python 3.12 và kế thừa PyTorch CUDA đã có từ môi trường
conda `dots_mocr`. Transformers 4.57.6, accelerate và qwen-vl-utils được cài
riêng trong venv; môi trường conda cũ không bị hạ phiên bản.

Model và tokenizer ở `weights/DotsMOCR`, tải hoàn toàn từ ổ đĩa khi chạy.
Các trọng số là symlink đến cache Hugging Face; giữ nguyên cache đó.
Hai file Python mô hình bổ sung được lấy từ dots-studio/dots.mocr.
`modeling_dots_vision.py` cho phép thiếu thư viện flash-attn khi chạy SDPA
có sẵn trong PyTorch. Không cần nvcc hay biên dịch flash-attn.

Ảnh đầu vào bị giới hạn 786432 pixel để phù hợp RTX 3060 12 GB. Nếu model
trả JSON hỏng, bị cắt hoặc thiếu VRAM, thử lại ảnh nhỏ hơn (524288 pixel).
Không lưu phản hồi chưa hoàn chỉnh thành cache thành công. Mỗi lần sinh tối đa
16000 token và có giới hạn thời gian sinh 240 giây; nạp model/render/prefill có
thể làm tổng thời gian trang dài hơn. Giảm ảnh có thể mất chi tiết chữ nhỏ.
Transformers có thể chậm hơn vLLM; chuyển backend không bảo đảm mọi số OCR đúng.

Backend vLLM cũ chỉ dùng khi chỉ định `--ocr-backend vllm`; menu dùng trực tiếp GPU.
Hướng dẫn model: https://huggingface.co/dots-studio/dots.mocr

## Kết quả và lưu trữ

Thư mục cố định `result/data/`:

- `Corporate_Bank_Loans.csv`: các dòng khoản vay, số tiền VND dạng số nguyên.
- `Extraction_Logs.csv`: trạng thái, tổng đã trích xuất, tổng nợ bảng cân đối và lý do.
- `extraction.sqlite3`: dữ liệu gốc để tái tạo CSV.

CSV UTF-8 BOM được tạo trước batch và cập nhật sau mỗi PDF. SQLite giao dịch từng
file; CSV xuất qua file tạm và thay thế nguyên tử. Chạy lại thay dữ liệu cùng PDF,
không nối thêm bản sao. Kết quả đã hoàn thành còn trên ổ đĩa khi dừng batch.
Cache từng trang lưu trong `ocr_cache_dots_mocr/<tên PDF>/<SHA-256>/pNNNN.json`.
Một trang lỗi không dừng các trang/chunk sau của PDF. Trang chưa xong được thử lại
ở lần chạy sau; trang cache hợp lệ không cần OCR lại.

SQLite dùng cache 4 MiB; CSV đọc từng dòng từ ổ đĩa. Parser vẫn giữ text của một
PDF trong RAM. Model và tensor ảnh dùng RAM/VRAM riêng. Không gom 105 PDF vào RAM.
Text đã đọc lưu trong `result/debug_text` khi chạy menu.

Danh tính PDF trong SQLite là đường dẫn tuyệt đối: thay nội dung cùng đường dẫn
thì chọn chạy lại. Cache cũ chưa có hash được nhận một lần nếu PDF có thời gian
sửa trước các cache; thời gian không chứng minh đúng PDF. Dùng `--ocr-cache`
trỏ sang thư mục mới nếu cần OCR hoàn toàn mới.

## Kiểm tra độ đúng

Tổng trong log bằng tổng số tiền VND của các dòng xuất CSV. Không chọn một tập
con để làm tổng gần bảng cân đối. Thiếu mã 320 hoặc 338 thì không giả định bằng 0.
Parser hỗ trợ mã trước/sau tên khoản mục và phân biệt khoản khác ngoại tệ.
Dòng tổng Ngắn hạn/Dài hạn không được xuất như tên bên cho vay. Nếu có bảng
tổng hợp theo ngân hàng cùng loại vay/ngoại tệ, dùng dòng tổng hợp và bỏ các
dòng chi tiết đã được tổng hợp; log ghi số dòng bỏ để tránh cộng hai lần.
Dòng Khoản vay có giá trị tài sản bảo đảm dùng số dư vay phía đầu dòng.

`[MISMATCH]`: tổng khác nợ bảng cân đối quá 5%. `[CHECK_MANUAL]`: thiếu dữ liệu hoặc
chưa đối chiếu được. `[OK]` chỉ là qua các kiểm tra hiện có, không chứng minh từng
khoản vay chính xác. Bảng OCR sai, khoản trùng/thiếu và cột không rõ vẫn cần xem tay.
Cảnh báo MuPDF `cmsOpenProfileFromMem failed` liên quan profile màu; các trang đã
kiểm tra vẫn render được. Xem lỗi trang và trạng thái CSV để đánh giá kết quả.

```bash
python -m unittest discover -v
```

Unit tests kiểm tra lỗi OCR không làm mất các chunk tiếp, cache, CSV/SQLite,
đối chiếu tổng, đọc mã bảng và chọn backend. OCR thật được kiểm tra riêng trên GPU.

Overflow protection: statement VND/date column headers take priority over units in introductory highlights. Amounts outside signed 64-bit range are left blank, preserved verbatim in warning notes, and flagged CHECK_MANUAL; they do not stop subsequent PDFs.

## Các file trên GitHub

Repo bao gồm mã nguồn, PDF trong `Source`, cache OCR và kết quả đã lưu.
Không đưa môi trường `.venv-dots-gpu`, Python cache, khóa truy cập hoặc trọng số
`*.safetensors` lên GitHub. Cấu hình/tokenizer và mã mô hình nhỏ vẫn có trong repo.
Trọng số dots.mocr lấy từ https://huggingface.co/dots-studio/dots.mocr;
cần tải riêng trước khi chạy trên máy khác. Môi trường GPU trong hướng dẫn phía
trên là cấu hình của máy hiện tại, không được đóng gói cùng repo.
