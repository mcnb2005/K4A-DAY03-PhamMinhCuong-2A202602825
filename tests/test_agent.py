"""Regression tests for behavior, native protocol continuity and honest evidence."""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from app import evaluate_case, load_test_cases, main, run_react_agent, save_waterfall_trace
from mcp_server import MCPAcademicServer
from providers import GeminiProvider, MockOfflineProvider, OpenAIProvider, ProviderError, get_llm_provider
from tools import MOCK_DATABASE, TOOLS_SCHEMA, dispatch_tool_call
from jsonschema import Draft202012Validator


class ScriptedProvider(MockOfflineProvider):
    """Replay protocol responses to exercise the real loop without a network call."""
    def __init__(self, turns):
        self.turns = iter(turns)
        self.histories = []

    def generate_with_tools(self, history, tools_schema, system_prompt=""):
        self.histories.append(copy.deepcopy(history))
        turn = next(self.turns)
        history.append({"role": "assistant", **turn})
        return turn


def tool_call(call_id, name="academic_query", arguments=None):
    return {"id": call_id, "name": name, "arguments": arguments if arguments is not None else {"student_id": "SV2026001"}}


class AgentTests(unittest.TestCase):
    def run_agent(self, provider, query="demo", **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_react_agent(query, provider, MCPAcademicServer(), **kwargs)

    def test_all_five_acceptance_cases_offline(self):
        for case in load_test_cases():
            with self.subTest(case=case["id"]):
                logs = self.run_agent(MockOfflineProvider(), case["question"], test_case_id=case["id"])
                result = evaluate_case(case, logs)
                self.assertTrue(result["passed"], result["failures"])
                self.assertTrue(all(entry["is_mock"] for entry in logs))

    def test_dynamic_advisor_comes_from_observation(self):
        case = load_test_cases()[3]
        with patch.dict(MOCK_DATABASE["SV2026002"], {"advisor": "TS. Cố Vấn Thay Đổi"}):
            logs = self.run_agent(MockOfflineProvider(), case["question"])
        booking = next(row for row in logs if row.get("tool_name") == "schedule_appointment")
        self.assertEqual(booking["arguments"]["advisor_name"], "TS. Cố Vấn Thay Đổi")
        self.assertEqual(booking["step"], 2)

    def test_multiple_calls_keep_all_observations_and_ids(self):
        provider = ScriptedProvider([
            {"calls": [tool_call("a"), tool_call("b", arguments={"student_id": "SV2026002"})], "content": ""},
            {"calls": [], "content": "Done"},
        ])
        logs = self.run_agent(provider)
        results = [m for m in provider.histories[1] if m.get("role") == "tool"]
        self.assertEqual([m["id"] for m in results], ["a", "b"])
        self.assertEqual(results[1]["observation"]["data"]["full_name"], "Trần Thị Bình")
        self.assertEqual(logs[-1]["action_type"], "FINAL_ANSWER")

    def test_invalid_json_is_observed_then_model_can_recover(self):
        provider = ScriptedProvider([
            {"calls": [tool_call("a", arguments="{invalid")], "content": ""},
            {"calls": [tool_call("b")], "content": ""},
            {"calls": [], "content": "Recovered"},
        ])
        logs = self.run_agent(provider)
        executions = [row for row in logs if row["action_type"] == "TOOL_EXECUTION"]
        self.assertEqual([row["observation"]["status"] for row in executions], ["INVALID_ARGUMENTS", "SUCCESS"])
        self.assertEqual(logs[-1]["output"], "Recovered")

    def test_unknown_tool_is_observed(self):
        provider = ScriptedProvider([
            {"calls": [tool_call("a", name="not_registered")], "content": ""},
            {"calls": [], "content": "Không có công cụ đó."},
        ])
        logs = self.run_agent(provider)
        self.assertEqual(logs[1]["observation"]["status"], "UNKNOWN_TOOL")
        self.assertEqual(provider.histories[1][-1]["observation"]["status"], "UNKNOWN_TOOL")

    def test_iteration_limit_is_failure_not_success(self):
        provider = ScriptedProvider([{"calls": [tool_call(str(i))], "content": ""} for i in range(2)])
        logs = self.run_agent(provider, max_iterations=2)
        self.assertEqual(logs[-1]["action_type"], "ITERATION_LIMIT")
        self.assertFalse(any(row["action_type"] == "FINAL_ANSWER" for row in logs))

    def test_provider_error_is_persisted_without_fallback(self):
        provider = MockOfflineProvider()
        provider.generate_with_tools = Mock(side_effect=ProviderError("Network failed"))
        logs = self.run_agent(provider)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["action_type"], "PROVIDER_ERROR")

    def test_trace_has_monotonic_timing_and_parent_ids(self):
        logs = self.run_agent(MockOfflineProvider(), load_test_cases()[3]["question"])
        known = set()
        previous_end = 0
        for row in logs:
            self.assertGreaterEqual(row["start_ms"], previous_end)
            self.assertGreaterEqual(row["latency_ms"], 0)
            self.assertAlmostEqual(row["end_ms"] - row["start_ms"], row["latency_ms"], delta=0.002)
            if "parent_event_id" in row:
                self.assertIn(row["parent_event_id"], known)
            known.add(row["event_id"])
            previous_end = row["end_ms"]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested/trace.json"
            with contextlib.redirect_stdout(io.StringIO()):
                save_waterfall_trace(logs, target)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), logs)

    def test_missing_student_or_date_does_not_book(self):
        for query in ["Đặt lịch vào 14:00 ngày 15/09/2026", "Đặt lịch cho SV2026001"]:
            logs = self.run_agent(MockOfflineProvider(), query)
            self.assertFalse(any(row["action_type"] == "TOOL_EXECUTION" for row in logs))

    def test_require_live_rejects_mock(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--all", "--provider", "mock", "--require-live"]), 2)

    def test_missing_live_key_does_not_silently_mock(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            with self.assertRaises(ProviderError):
                get_llm_provider("gemini")

    def test_vietnamese_interactive_input_from_pipe(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(root / "src/app.py"), "--interactive", "--provider", "mock",
                 "--trace", str(Path(directory) / "session.json")],
                input="Hãy tra cứu hồ sơ của SV2026001.\nexit\n", text=True, encoding="utf-8",
                capture_output=True, cwd=directory, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nguyễn Văn An", result.stdout)


class ToolTests(unittest.TestCase):
    def test_schemas_and_validation(self):
        for tool in TOOLS_SCHEMA:
            Draft202012Validator.check_schema(tool["parameters"])
        for arguments in [{}, {"student_id": 123}, {"student_id": " "}, {"student_id": "SV2026001", "extra": True}]:
            self.assertEqual(json.loads(dispatch_tool_call("academic_query", arguments))["status"], "INVALID_ARGUMENTS")

    def test_not_found_keeps_requested_id(self):
        result = json.loads(dispatch_tool_call("academic_query", {"student_id": "sv9999999"}))
        self.assertEqual(result["status"], "NOT_FOUND")
        self.assertEqual(result["student_id"], "SV9999999")
        self.assertNotIn("data", result)

    def test_booking_validation_and_idempotency(self):
        server = MCPAcademicServer()
        valid = {"student_id": "SV2026001", "datetime_str": "14:00 15/09/2026", "advisor_name": "PGS.TS Nguyễn Văn A"}
        self.assertEqual(server.call_tool("schedule_appointment", {**valid, "datetime_str": "14:00 31/02/2026"})["result"]["status"], "INVALID_ARGUMENTS")
        self.assertEqual(server.call_tool("schedule_appointment", {**valid, "advisor_name": "Guess"})["result"]["status"], "ADVISOR_MISMATCH")
        self.assertEqual(server.call_tool("schedule_appointment", {**valid, "student_id": "SV9999999"})["result"]["status"], "NOT_FOUND")
        first = server.call_tool("schedule_appointment", valid)["result"]
        repeat = server.call_tool("schedule_appointment", valid)["result"]
        self.assertEqual(first["booking_id"], repeat["booking_id"])
        self.assertTrue(repeat["already_booked"])
        self.assertEqual(len(server.bookings), 1)
        self.assertEqual(len(MCPAcademicServer().bookings), 0)

    def test_json_rpc_errors_and_request_ids(self):
        server = MCPAcademicServer()
        self.assertEqual(server.handle_request([])["error"]["code"], -32600)
        self.assertEqual(server.handle_request({"jsonrpc": "2.0", "id": "r", "method": "unknown"})["error"]["code"], -32601)
        self.assertEqual(server.handle_request({"jsonrpc": "2.0", "id": "r", "method": "tools/call", "params": []})["error"]["code"], -32602)
        listed = server.handle_request({"jsonrpc": "2.0", "id": "r", "method": "tools/list"})
        self.assertEqual(listed["id"], "r")
        self.assertEqual(len(listed["result"]["tools"]), 2)
        self.assertIn("inputSchema", listed["result"]["tools"][0])
        a = server.call_tool("academic_query", {"student_id": "SV2026001"})
        b = server.call_tool("academic_query", {"student_id": "SV2026001"})
        self.assertNotEqual(a["id"], b["id"])


class NativeAdapterTests(unittest.TestCase):
    def test_openai_preserves_original_output_and_all_call_ids(self):
        # Real SDK response models, fake HTTP client: protocol test, NOT live evidence.
        from openai.types.responses import Response
        response = Response.model_validate({
            "id": "resp_test", "created_at": 0, "model": "gpt-4o-mini", "object": "response", "status": "completed",
            "output": [{"type": "function_call", "call_id": "call_a", "name": "academic_query", "arguments": '{"student_id":"SV2026001"}'}],
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        })
        client = Mock()
        client.responses.create.return_value = response
        provider = OpenAIProvider(client=client)
        history = provider.start_session("demo")
        turn = provider.generate_with_tools(history, TOOLS_SCHEMA, "system")
        self.assertIs(history[1], response.output[0])
        provider.observe(history, [{**turn["calls"][0], "observation": {"status": "SUCCESS"}}])
        self.assertEqual(history[2]["call_id"], "call_a")
        self.assertEqual(history[2]["type"], "function_call_output")
        self.assertTrue(client.responses.create.call_args.kwargs["tools"][0]["strict"])

    def test_gemini_preserves_signature_and_function_response(self):
        from google.genai import types
        model_content = types.Content(role="model", parts=[types.Part(
            thought_signature=b"opaque-signature", function_call=types.FunctionCall(
                id="native-a", name="academic_query", args={"student_id": "SV2026001"}))])
        response = types.GenerateContentResponse(candidates=[types.Candidate(content=model_content, finish_reason="STOP")])
        client = Mock()
        client.models.generate_content.return_value = response
        provider = GeminiProvider(client=client)
        history = provider.start_session("demo")
        turn = provider.generate_with_tools(history, TOOLS_SCHEMA, "system")
        self.assertIs(history[1], response.candidates[0].content)
        self.assertEqual(history[1].parts[0].thought_signature, b"opaque-signature")
        provider.observe(history, [{**turn["calls"][0], "observation": {"status": "SUCCESS"}}])
        self.assertEqual(history[2].parts[0].function_response.id, "native-a")
        self.assertEqual(history[2].parts[0].function_response.response["status"], "SUCCESS")
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertTrue(config.automatic_function_calling.disable)

    def test_api_exception_redacts_secret(self):
        client = Mock()
        client.responses.create.side_effect = RuntimeError("URL contains SUPER_SECRET_KEY")
        provider = OpenAIProvider(client=client)
        with self.assertRaises(ProviderError) as caught:
            provider.generate_with_tools(provider.start_session("demo"), TOOLS_SCHEMA)
        self.assertNotIn("SUPER_SECRET_KEY", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
