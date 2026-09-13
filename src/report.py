"""Build an evidence-based Vietnamese report from the actual saved run, without API calls."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_report(trace_path):
    trace_path = Path(trace_path)
    logs = json.loads(trace_path.read_text(encoding="utf-8-sig"))
    summary_path = trace_path.with_name(trace_path.stem + ".eval.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    run_ids = list(dict.fromkeys(row["run_id"] for row in logs))
    if summary.get("run_ids") != run_ids:
        raise ValueError("Trace and evaluation belong to different runs. Run the full suite again.")
    if any(row["is_mock"] != summary["is_mock"] or row["provider"] != summary["provider"] for row in logs):
        raise ValueError("Trace provenance does not match the evaluation.")
    is_mock = summary["is_mock"]
    mode = "OFFLINE MOCK — chưa nghiệm thu API thật" if is_mock else "LLM API thật"
    tools = [row for row in logs if row["action_type"] == "TOOL_EXECUTION"]
    excerpt = [row for row in logs if row["test_case_id"] == "TC04" and row["action_type"] in ("TOOL_EXECUTION", "FINAL_ANSWER")]
    # Project fields from the recorded events; no synthesized results or latencies.
    fields = ("step", "provider", "model", "is_mock", "action_type", "tool_name", "arguments", "observation", "output", "latency_ms")
    excerpt = [{key: row[key] for key in fields if key in row} for row in excerpt]
    table = "\n".join(
        f"| {row['test_case_id']} | {'PASS' if row['passed'] else 'FAIL'} | {' → '.join(row['tool_sequence']) or 'Không gọi tool'} | {row['llm_turns']} | {row['latency_ms']:.3f} |"
        for row in summary["cases"]
    )
    failures = "\n".join(f"- {row['test_case_id']}: {'; '.join(row['failures'])}" for row in summary["cases"] if row["failures"])
    baseline_path = trace_path.with_name(trace_path.stem + ".baseline.json")
    baseline_note = "Chưa có kết quả chạy baseline đi kèm lần nghiệm thu này."
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8-sig"))
        baseline_note = (f"Đã chạy Chatbot Baseline trên cùng {len(baseline)} câu hỏi; "
                         f"{sum(row['status'] == 'COMPLETED' for row in baseline)} phản hồi hoàn tất, "
                         f"{sum(row['tool_count'] for row in baseline)} lượt tool. "
                         f"Bằng chứng: [{baseline_path.name}]({baseline_path.name}).")
    live_pass = summary["live_acceptance_passed"]
    checked = "x" if live_pass else " "
    report = f"""# Báo cáo Bài Lab 3 — Chatbot vs ReAct Agent

**Họ và tên:** Phạm Minh Cương<br>
**Mã học viên:** 2A202602825<br>
**Lớp:** K4A — buổi sáng<br>
**Chủ đề:** Trợ lý học vụ — tra cứu hồ sơ và đặt lịch tư vấn mô phỏng<br>
**Repository:** https://github.com/mcnb2005/K4A-DAY03-PhamMinhCuong-2A202602825<br>
**Hình thức:** Cá nhân (`workMode: individual`)

## 1. Agentic Fit Scoring Matrix

| Tiêu chí | Điểm | Giải trình |
| --- | ---: | --- |
| Multi-step Reasoning | 5/5 | TC04 cần tra hồ sơ, lấy cố vấn từ Observation, đặt lịch rồi tổng hợp hồ sơ và mã hẹn. |
| Tool Interaction | 5/5 | LLM không chứa cơ sở dữ liệu học vụ hoặc tự tạo booking; phải gọi hai công cụ qua MCP simulator. |
| Dynamic Decision | 4/5 | Có hồ sơ thì tiếp tục đặt lịch; NOT_FOUND thì dừng; thiếu ngày giờ thì hỏi lại. Quyết định sau phụ thuộc kết quả trước. |
| Long Horizon Goal | 2/5 | Giữ mục tiêu qua 2–3 lượt LLM trong một yêu cầu; chưa có kế hoạch dài hạn, học tự động hay memory bền vững. |
| **Tổng** | **16/20** | Phù hợp ReAct Agent cấp 3. Câu hỏi quy chế chung TC01 vẫn phù hợp Chatbot cấp 2. |

## 2. Thiết kế và thay đổi so với starter

- Hoàn thiện JSON Schema cho `schedule_appointment`, yêu cầu đủ ba tham số, từ chối trường thừa/sai kiểu.
- `MCPAcademicServer.call_tool()` gửi request JSON-RPC, dispatch tool và trả kết quả có ID.
- Vòng lặp gửi toàn bộ Observation về LLM và gọi tiếp đến Final Answer hoặc tối đa 5 lượt.
  TC04 dùng cố vấn từ hồ sơ, không tự điền tên cố vấn vào code Agent.
- OpenAI sử dụng Responses API và `function_call_output`; Gemini sử dụng native
  FunctionCall/FunctionResponse, giữ nguyên Content và thought signature trong lịch sử.
- Xử lý tất cả tool calls trong một phản hồi; provider thật lỗi thì báo lỗi, không fallback mock.
- Baseline cùng provider chỉ sinh text, không có tool. `--compare` lưu câu trả lời để đối chiếu.
- Mọi hồ sơ và lịch hẹn đều mô phỏng. MCP là simulator trong tiến trình theo phạm vi starter,
  chưa phải MCP SDK server có handshake và transport stdio/HTTP hoàn chỉnh.

## 3. Kết quả nghiệm thu từ file thực thi

**Trạng thái:** {mode}<br>
**Provider / model:** `{summary['provider']}` / `{summary['model']}`<br>
**Thời điểm sinh tổng kết (UTC):** `{summary['generated_at']}`<br>
**Test cases đạt điều kiện tự động:** {summary['passed']}/{summary['total']}<br>
**Tổng lượt tool:** {len(tools)}; trong đó SUCCESS: {sum(row['observation'].get('status') == 'SUCCESS' for row in tools)}, NOT_FOUND: {sum(row['observation'].get('status') == 'NOT_FOUND' for row in tools)}.

| Test | Kết quả | Chuỗi công cụ | Lượt LLM | Thời gian sự kiện (ms) |
| --- | --- | --- | ---: | ---: |
{table}

{failures or 'Không có assertion thất bại trong lần chạy này.'}

Bằng chứng: [{trace_path.name}]({trace_path.name}), [{summary_path.name}]({summary_path.name}).
{'**Log mock chỉ chứng minh luồng phần mềm; chưa chứng minh Native Tool Calling với LLM thật. Cần chạy `--all --require-live --compare` sau khi cấu hình API để sinh `docs/trace_waterfall.json`.**' if is_mock else 'Trace trên lấy từ API thật; công cụ và hồ sơ vẫn là dữ liệu mô phỏng của bài lab.'}

{baseline_note}

Baseline không thể tra hồ sơ hay thực hiện đặt lịch. Agent thực hiện được qua tool,
đổi lại cần thêm lượt LLM và thời gian API. PASS tự động kiểm tra tên tool, tham số,
thứ tự phụ thuộc Observation, trạng thái và các thông tin chính trong câu trả lời;
không thay thế hoàn toàn đánh giá chất lượng ngôn ngữ bởi người đọc.

## 4. Trích đoạn Waterfall Trace — TC04

Trích các trường từ sự kiện đã lưu, giữ nguyên dữ liệu và thời gian đo được:

```json
{json.dumps(excerpt, ensure_ascii=False, indent=2)}
```

`thought` ở sự kiện LLM_DECISION là tóm tắt quyết định do ứng dụng tạo
(`thought_source=application_summary`), không phải chain-of-thought nội bộ.
`latency_ms` đo riêng LLM/tool bằng `perf_counter`; FINAL_ANSWER chỉ đo ghi nhận text đã nhận,
nên thường gần 0 ms. Xem [định dạng trace](TRACE_FORMAT.md).

## 5. Tự kiểm tra và giới hạn

- [x] Fork đúng starter K4A, tên repo theo họ tên/mã học viên.
- [x] Hoàn thiện 5 câu hỏi TC01–TC05 và hai tool schemas.
- [x] Kiểm thử offline vòng lặp, nhiều tool calls, giữ native IDs/signatures, lỗi JSON/tool/API, giới hạn vòng lặp, UTF-8 và idempotency.
- [{checked}] Nghiệm thu API thật đạt 5/5 test cases; có trace thật tại `docs/trace_waterfall.json`.
- [ ] Nộp URL repository vào LMS VLearn (thao tác của học viên, chưa xác minh).

Bộ regression: `python -m unittest discover -s tests -v`. Kết quả kiểm thử gần nhất
được ghi trong [validation.md](validation.md). File `.env` chứa khóa API không đưa vào Git.
Lịch mô phỏng lưu trong RAM, không giữ qua lần khởi động mới. Chưa triển khai phân quyền
sinh viên, lịch thật, memory dài hạn, truy xuất văn bản quy chế chính thức hoặc Autonomous Agent cấp 4.
Không suy rộng ngưỡng tín chỉ/GPA thành quy chế VinUni khi chưa có tài liệu nguồn.

## 6. Nguồn tham khảo

- [Starter K4A](https://github.com/VinUni-AI20k/K4A-Day03-Lab-Chatbot-vs-ReAct-Agent-MCP) và [CODELAB gốc](CODELAB.md).
- [OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling): trả tool output đúng call_id và gọi LLM tiếp.
- [Gemini Thought signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures): giữ nguyên Content model trong lịch sử qua các bước.
"""
    target = ROOT / "docs/trace_eval.md"
    target.write_text(report, encoding="utf-8")
    print(f"Report: {target}; mock={is_mock}; live_pass={live_pass}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the lab report from saved trace evidence")
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    path = args.trace or ROOT / "docs/trace_waterfall.json"
    if not path.exists() and args.trace is None:
        path = ROOT / "docs/trace_waterfall.mock.json"
    build_report(path)
