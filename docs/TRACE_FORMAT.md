# Cách đọc Waterfall Trace

File trace là mảng các sự kiện, nhóm bằng `run_id` và `test_case_id`.
Không sử dụng suy luận nội bộ ẩn của LLM làm bằng chứng. Bằng chứng là phản hồi native,
tham số, kết quả thực thi và lượt LLM tiếp theo sử dụng kết quả đó.

| Trường | Ý nghĩa |
| --- | --- |
| `provider`, `model`, `is_mock` | Nguồn thực thi; `is_mock=true` không phải bằng chứng LLM thật |
| `event_id`, `parent_event_id` | Liên kết sự kiện tool/final với quyết định LLM sinh ra nó |
| `step` | Số lượt gọi LLM trong tác vụ, tối đa 5 |
| `LLM_DECISION` | Một lượt LLM, gồm các `proposed_calls`, token usage và response ID nếu API trả |
| `thought`, `thought_source` | Tóm tắt hành động ở mức cao do ứng dụng tạo; không phải nội dung suy luận ẩn |
| `TOOL_EXECUTION` | Action và Observation; có `tool_name`, `arguments`, `tool_call_id` và `mcp_response` |
| `FINAL_ANSWER` | Văn bản thực sự do provider trả về, không ghép cứng trong vòng lặp |
| `PROVIDER_ERROR`, `ITERATION_LIMIT` | Kết thúc không thành công, không được tính PASS |
| `started_at`, `ended_at` | Dấu thời gian UTC theo ISO 8601 |
| `start_ms`, `end_ms`, `latency_ms` | Khoảng thời gian thực từ `perf_counter`, tương đối với đầu tác vụ |

`LLM_DECISION.latency_ms` gồm lượt gọi API; `TOOL_EXECUTION.latency_ms` đo dispatch tool.
`FINAL_ANSWER` chỉ đo ghi nhận kết quả đã có, nên thường gần 0 ms. Không cộng thêm một
latency giả cho việc tổng hợp. Thời gian tương đối không bị sai khi đồng hồ hệ thống thay đổi.

Để kiểm tra TC04:

1. Lượt 1 đề xuất `academic_query` với `student_id=SV2026002`.
2. Observation có `data.advisor=TS. Lê Thị B` và GPA 3.6.
3. Lượt 2 đề xuất `schedule_appointment` dùng chính tên cố vấn và giờ người dùng yêu cầu.
4. Observation có `booking_id` và trạng thái `SUCCESS`.
5. Lượt 3 trả câu trả lời cuối gồm hồ sơ và lịch đã đặt mô phỏng.

OpenAI giữ nguyên các response output và gửi `function_call_output` theo `call_id`.
Gemini giữ nguyên `Content` của model (kể cả chữ ký suy luận opaque) trong bộ nhớ và gửi
`FunctionResponse` theo tên/ID. Log không xuất chữ ký hay nội dung suy luận ẩn.

MCP ở đây là mô phỏng từ starter: `tools/list`, `tools/call` với JSON-RPC 2.0 và request ID.
Tool errors (NOT_FOUND, INVALID_ARGUMENTS, UNKNOWN_TOOL...) là dữ liệu Observation;
lỗi cấu trúc giao thức sử dụng JSON-RPC `error`. Chưa có handshake/transport MCP SDK đầy đủ.
