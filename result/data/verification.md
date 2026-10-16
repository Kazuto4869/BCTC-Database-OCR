# Kiểm tra xuất CSV và dữ liệu đã khôi phục

Ngày kiểm tra: 12/10/2026.

## Lưu trữ

- Corporate_Bank_Loans.csv: 142 dòng, từng ô khớp SQLite.
- Extraction_Logs.csv: 7 PDF, từng ô khớp SQLite.
- SQLite: integrity_check = ok; không có lỗi khóa ngoại.
- CSV UTF-8 BOM, tiền được ghi dưới dạng số nguyên; dấu phẩy/xuống dòng trong tên được quote.
- Không còn xuất XLSX. CSV có header trước PDF đầu tiên và cập nhật sau mỗi PDF.
- Mặc định bỏ qua PDF đã lưu; chọn chế độ 2 hoặc --rerun để chạy lại và thay kết quả cũ.
- PDF lỗi đọc/OCR vẫn được thử lại. PDF cùng đường dẫn nhưng đổi nội dung cần chọn chạy lại.
- SQLite lưu từng PDF bằng transaction; CSV đọc qua cursor, không gom cả lô vào RAM.
- Cache OCR mới tách theo SHA-256 nội dung; cache cũ chỉ được tiếp nhận theo giả định thời gian PDF cũ hơn cache.

## Độ phủ và đối chiếu BCM

| File | Trang JSON parse được / tổng | Dòng khoản vay | Tổng dùng đối chiếu (VND) | Nợ vay CĐKT (VND) | Lệch | Cờ |
|---|---:|---:|---:|---:|---:|---|
| BCM_2021.pdf | 80/82 | 23 | 8,405,771,006,929 | 16,634,518,093,006 | -49.47% | [MISMATCH] |
| BCM_2022.pdf | 76/77 | 18 | 18,600,692,374,551 | 15,886,382,366,428 | +17.09% | [MISMATCH] |
| BCM_2023.pdf | 72/75 | 18 | 9,764,738,632,124 | 19,716,441,961,546 | -50.47% | [MISMATCH] |
| BCM_2024.pdf | 75/78 | 12 | 8,333,287,064,668 | 23,628,027,997,388 | -64.73% | [MISMATCH] |
| BCM_2025.pdf | 91/92 | 14 | 18,101,464,062,775 | 23,947,890,397,016 | -24.41% | [MISMATCH] |

**Chưa thể kết luận BCM được bóc đúng hết. Cả 5 năm vẫn MISMATCH.**

Đã kiểm tra trực tiếp ảnh PDF BCM_2021 trang 13: mã 320 là 4.248.089.036.214 VND; mã 338 là 12.386.429.056.792 VND. Tổng trong log là 16.634.518.093.006 VND, khớp hai mã này.

Các lỗi đã sửa: chọn nhầm bảng điều chỉnh; tham chiếu V.24a/V.23a,c; CĐKT dài hơn ba trang; tiêu đề 23. Vay/23a/23b; dòng tổng nhóm bị đọc thành chi tiết; dòng giải thích bị ghép với số rác 498.

Phần cần kiểm tra tiếp: bảng trái phiếu nối nhiều trang, chi phí phát hành âm, các bên cho vay/tên ngân hàng bị OCR sai và các dòng chi tiết bị mất hoặc chọn nhầm cột. Số dòng CSV đúng với SQLite không chứng minh từng khoản vay đúng với PDF.

CII_2021 có cờ OK theo phép đối chiếu tổng; cờ OK không chứng minh từng dòng đúng. CII_2022 có MISMATCH và cache JSON lỗi ở trang 49. Các cảnh báo trang cache lỗi đã được ghi vào Extraction_Logs.csv.

## Giới hạn bộ nhớ và kiểm thử

- Lượt xử lý lại 7 PDF từ cache: `PEAK_RSS_KB=155416 ELAPSED_SECONDS=5.06`.
- Đây là RAM của tiến trình trích xuất từ cache, không gồm server vLLM hoặc việc render/request OCR mới.
- Bộ phân tích vẫn cần các trang của một PDF trong RAM. Mặc định workers=1 và ocr-threads=2 giới hạn số PDF/ảnh đồng thời.
- 29 tests đạt: persistence, rollback, CSV header/Unicode, chạy song song, skip/rerun, cache theo nội dung, và các trường hợp parser đã sửa.

- Số trang BCM có raw layout JSON không hợp lệ: 10.
  BCM_2021.pdf: trang 18
  BCM_2021.pdf: trang 62
  BCM_2022.pdf: trang 45
  BCM_2023.pdf: trang 8
  BCM_2023.pdf: trang 30
  BCM_2023.pdf: trang 47
  BCM_2024.pdf: trang 5
  BCM_2024.pdf: trang 42
  BCM_2024.pdf: trang 52
  BCM_2025.pdf: trang 51

Khi chọn chạy lại với server hoạt động, các trang cache JSON lỗi sẽ được yêu cầu OCR lại, còn trang hợp lệ được dùng lại. Phản hồi mới hết token hoặc sai JSON không được lưu thành cache thành công.
