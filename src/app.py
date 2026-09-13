"""Lab CLI: LLM -> native calls -> MCP observations -> LLM, with measured traces."""
import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mcp_server import MCPAcademicServer
from prompts import CHATBOT_BASELINE_PROMPT, REACT_AGENT_SYSTEM_PROMPT, MAX_ITERATIONS
from providers import ProviderError, get_llm_provider

ROOT = Path(__file__).resolve().parents[1]


def load_test_cases():
    with (ROOT / "config/test_cases.json").open(encoding="utf-8-sig") as file:
        cases = json.load(file)
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate test case IDs")
    if any(not case["question"].strip() or case["question"].startswith("TODO") for case in cases):
        raise ValueError("Every test case must have a real question")
    return cases


def save_waterfall_trace(trace_data, path=None):
    path = Path(path) if path else ROOT / "docs/trace_waterfall.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(trace_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    print(f"[TRACE] {path}")


def run_baseline_chatbot(user_query, provider):
    start = time.perf_counter()
    try:
        content = provider.generate(user_query, system_prompt=CHATBOT_BASELINE_PROMPT)
        result = {"status": "COMPLETED", "output": content}
    except ProviderError as exc:
        result = {"status": "ERROR", "error": str(exc)}
    return {**result, "latency_ms": round((time.perf_counter() - start) * 1000, 3),
            "provider": provider.provider_name, "model": provider.model_name,
            "is_mock": provider.is_mock, "tool_count": 0}


def run_react_agent(user_query, provider, mcp_server, *, test_case_id=None, max_iterations=MAX_ITERATIONS):
    if max_iterations < 1:
        raise ValueError("max_iterations must be positive")
    run_id = uuid.uuid4().hex
    run_start = time.perf_counter()
    logs = []
    history = provider.start_session(user_query)
    tools_list = mcp_server.list_tools()

    def stamp():
        return time.perf_counter(), datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def event(step, action_type, started, **fields):
        end = time.perf_counter()
        record = {
            "run_id": run_id, "test_case_id": test_case_id, "event_id": f"{run_id}:{len(logs) + 1}",
            "step": step, "query": user_query, "action_type": action_type,
            "provider": provider.provider_name, "model": provider.model_name, "is_mock": provider.is_mock,
            "started_at": started[1], "ended_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "start_ms": round((started[0] - run_start) * 1000, 3),
            "end_ms": round((end - run_start) * 1000, 3),
            "latency_ms": round((end - started[0]) * 1000, 3), **fields,
        }
        logs.append(record)
        return record["event_id"]

    print(f"\n[{test_case_id or 'CHAT'}] {user_query}", flush=True)
    for step in range(1, max_iterations + 1):
        started = stamp()
        try:
            response = provider.generate_with_tools(history, tools_list, system_prompt=REACT_AGENT_SYSTEM_PROMPT)
        except ProviderError as exc:
            event(step, "PROVIDER_ERROR", started, error=str(exc))
            print(f"[ERROR] {exc}", flush=True)
            break
        calls = response.get("calls", [])
        # A high-level description of the observable decision, not hidden chain-of-thought.
        summary = ("Yêu cầu thực thi công cụ: " + ", ".join(call["name"] for call in calls)
                   if calls else "LLM trả lời bằng văn bản dựa trên ngữ cảnh hiện có.")
        decision_id = event(step, "LLM_DECISION", started, thought=summary,
                            thought_source="application_summary", proposed_calls=calls,
                            usage=response.get("usage", {}), response_id=response.get("response_id"))
        if not calls:
            text = response.get("content", "").strip()
            if not text:
                event(step, "PROVIDER_ERROR", stamp(), error="LLM returned neither text nor tool calls.", parent_event_id=decision_id)
                break
            event(step, "FINAL_ANSWER", stamp(), output=text, parent_event_id=decision_id)
            print(f"[FINAL] {text}", flush=True)
            break
        results = []
        for call in calls:
            started = stamp()
            arguments = call.get("arguments", {})
            mcp_result = None
            try:
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be a JSON object")
                mcp_result = mcp_server.call_tool(call["name"], arguments)
                observation = mcp_result.get("result")
                if observation is None:
                    observation = {"status": "MCP_ERROR", "message": mcp_result.get("error", {}).get("message", "Missing MCP result")}
            except (ValueError, TypeError):
                observation = {"status": "INVALID_ARGUMENTS", "message": "Tham số tool phải là JSON object hợp lệ."}
            except Exception as exc:
                observation = {"status": "EXECUTION_ERROR", "message": f"MCP execution failed ({type(exc).__name__})."}
            event(step, "TOOL_EXECUTION", started, parent_event_id=decision_id,
                  tool_call_id=call["id"], tool_name=call["name"], arguments=arguments,
                  observation=observation, mcp_response=mcp_result, transport="in_process_simulator")
            results.append({**call, "observation": observation})
            print(f"[STEP {step}] {call['name']} -> {observation.get('status')}", flush=True)
        # Send ALL call results back using provider-native tool messages before next LLM step.
        provider.observe(history, results)
    else:
        event(max_iterations, "ITERATION_LIMIT", stamp(), error=f"Stopped after {max_iterations} LLM turns without a final answer.")
        print("[LIMIT] Agent stopped without a final answer.", flush=True)
    return logs


def evaluate_case(case, logs):
    """Structural assertions plus answer fragments; not a substitute for human review."""
    executions = [entry for entry in logs if entry["action_type"] == "TOOL_EXECUTION"]
    finals = [entry for entry in logs if entry["action_type"] == "FINAL_ANSWER"]
    names = [entry["tool_name"] for entry in executions]
    failures = []
    if not finals:
        failures.append("No final answer")
    if any(entry["action_type"] in ("PROVIDER_ERROR", "ITERATION_LIMIT") for entry in logs):
        failures.append("Provider failure or iteration limit")
    if names not in case["allowed_tool_sequences"]:
        failures.append(f"Unexpected tool sequence: {names}")
    for entry in executions:
        expected = case.get("expected_arguments", {}).get(entry["tool_name"], {})
        arguments = entry["arguments"]
        if not isinstance(arguments, dict) or any(arguments.get(k) != v for k, v in expected.items()):
            failures.append(f"Wrong arguments: {entry['tool_name']}")
        expected_status = case.get("expected_status", {}).get(entry["tool_name"], "SUCCESS")
        if entry["observation"].get("status") != expected_status:
            failures.append(f"Wrong observation: {entry['tool_name']}")
    if case.get("requires_observation_dependency"):
        queries = [entry for entry in executions if entry["tool_name"] == "academic_query"]
        bookings = [entry for entry in executions if entry["tool_name"] == "schedule_appointment"]
        if not queries or not bookings or bookings[0]["step"] <= queries[0]["step"]:
            failures.append("Booking must follow an observation in a later LLM turn")
        elif bookings[0]["arguments"].get("advisor_name") != queries[0]["observation"].get("data", {}).get("advisor"):
            failures.append("Advisor was not taken from observation")
    final_text = finals[-1]["output"].casefold() if finals else ""
    for alternatives in case.get("answer_checks", []):
        if not any(fragment.casefold() in final_text for fragment in alternatives):
            failures.append(f"Missing answer evidence: {alternatives}")
    for entry in executions:
        booking_id = entry["observation"].get("booking_id")
        if booking_id and booking_id.casefold() not in final_text:
            failures.append("Final answer omits booking_id")
    for forbidden in case.get("forbidden_answer_fragments", []):
        if forbidden.casefold() in final_text:
            failures.append(f"Unsupported answer fragment: {forbidden}")
    return {"test_case_id": case["id"], "passed": not failures, "failures": failures,
            "tool_sequence": names, "tool_count": len(executions),
            "llm_turns": sum(entry["action_type"] == "LLM_DECISION" for entry in logs),
            "latency_ms": round(sum(entry["latency_ms"] for entry in logs), 3)}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="K4A Day03: Chatbot vs ReAct Agent")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="Run the five acceptance cases")
    mode.add_argument("--interactive", action="store_true")
    mode.add_argument("--query", help="Run one custom question")
    parser.add_argument("--provider", choices=["mock", "gemini", "openai"])
    parser.add_argument("--require-live", action="store_true", help="Refuse mock evidence")
    parser.add_argument("--compare", action="store_true", help="Also run the tool-free baseline")
    parser.add_argument("--trace", type=Path, help="Override trace path")
    args = parser.parse_args(argv)
    try:
        provider = get_llm_provider(args.provider)
        if args.require_live and provider.is_mock:
            raise ProviderError("Live acceptance requires gemini/openai with an API key; mock evidence rejected.")
    except ProviderError as exc:
        print(f"[CONFIG ERROR] {exc}")
        return 2
    print(f"Provider={provider.provider_name}; model={provider.model_name}; is_mock={provider.is_mock}", flush=True)
    suffix = ".mock" if provider.is_mock else ""
    default_name = f"trace_waterfall{suffix}.json" if args.all else f"trace_session{suffix}.json"
    trace_path = args.trace or ROOT / "docs" / default_name
    if args.interactive:
        print("Gõ exit hoặc quit để thoát. Mỗi câu hỏi là một tác vụ độc lập; lịch mô phỏng được giữ trong phiên.")
        all_logs, server = [], MCPAcademicServer()
        while True:
            try:
                query = input("Bạn: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if query.lower() in ("exit", "quit"):
                break
            if not query:
                continue
            all_logs.extend(run_react_agent(query, provider, server))
            save_waterfall_trace(all_logs, trace_path)
        return 0
    if not args.all:
        query = args.query or load_test_cases()[1]["question"]
        if args.compare:
            print(json.dumps(run_baseline_chatbot(query, provider), ensure_ascii=False, indent=2))
        logs = run_react_agent(query, provider, MCPAcademicServer())
        save_waterfall_trace(logs, trace_path)
        return 0 if logs[-1]["action_type"] == "FINAL_ANSWER" else 1
    all_logs, evaluations, baselines = [], [], []
    for case in load_test_cases():
        # Fresh server per case prevents cross-test appointment state leakage.
        logs = run_react_agent(case["question"], provider, MCPAcademicServer(), test_case_id=case["id"])
        all_logs.extend(logs)
        evaluations.append(evaluate_case(case, logs))
        if args.compare:
            baselines.append({"test_case_id": case["id"], "query": case["question"],
                              **run_baseline_chatbot(case["question"], provider)})
        save_waterfall_trace(all_logs, trace_path)  # Preserve evidence even when a later API call fails.
    summary = {"provider": provider.provider_name, "model": provider.model_name, "is_mock": provider.is_mock,
               "generated_at": datetime.now(timezone.utc).isoformat(),
               "run_ids": list(dict.fromkeys(entry["run_id"] for entry in all_logs)),
               "passed": sum(result["passed"] for result in evaluations), "total": len(evaluations),
               "tool_calls": sum(result["tool_count"] for result in evaluations), "cases": evaluations}
    summary["live_acceptance_passed"] = not provider.is_mock and summary["passed"] == summary["total"]
    save_waterfall_trace(summary, trace_path.with_name(trace_path.stem + ".eval.json"))
    if baselines:
        save_waterfall_trace(baselines, trace_path.with_name(trace_path.stem + ".baseline.json"))
    print(f"[RESULT] {summary['passed']}/{summary['total']} PASS; tool calls={summary['tool_calls']}; live={summary['live_acceptance_passed']}")
    return 0 if summary["passed"] == summary["total"] and all(x["status"] == "COMPLETED" for x in baselines) else 1


if __name__ == "__main__":
    sys.exit(main())
