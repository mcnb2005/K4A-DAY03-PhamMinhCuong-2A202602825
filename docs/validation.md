# Kiểm chứng cục bộ — 13/09/2026

Môi trường: Windows PowerShell, Python 3.12.14 trong `.venv`.

| Kiểm tra đã thực hiện | Kết quả |
| --- | --- |
| `python -m unittest discover -s tests -v` | 19 tests, OK |
| `python src/app.py --all --provider mock --compare` | 5/5 PASS, 5 lượt tool; is_mock=true |
| `python src/mcp_server.py` | Công bố 2 tools, JSON-RPC tra hồ sơ SUCCESS |
| Interactive CLI qua stdin UTF-8 (test subprocess, chạy từ thư mục khác) | Nhận đúng tiếng Việt và trả Nguyễn Văn An |
| Native adapter với đối tượng thật của OpenAI/Google SDK, HTTP client mô phỏng | Giữ call_id, Content/thought_signature, FunctionResponse |
| `--require-live` với mock | Exit code 2; từ chối lấy mock làm bằng chứng API thật |

19 kiểm tra hồi quy bao gồm lỗi tham số, ngày không hợp lệ, mã không tồn tại,
khớp cố vấn, đặt lịch lặp, JSON-RPC lỗi, nhiều calls, phục hồi sau JSON lỗi,
đọc tên cố vấn thay đổi từ Observation, giới hạn vòng lặp và che nội dung lỗi SDK
có thể chứa khóa API.

Kiểm tra adapter với HTTP client mô phỏng chỉ xác minh cấu trúc giao thức;
không được tính là kết nối API thật. Trạng thái nghiệm thu API thật xem `trace_eval.md`.
