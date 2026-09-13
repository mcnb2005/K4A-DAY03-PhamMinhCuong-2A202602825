"""Native tool adapters with explicit conversation history and no silent fallback.

A turn appends the ORIGINAL provider response to history; observe() then adds all
matching tool results. This preserves call IDs and Gemini thought signatures.
Only MockOfflineProvider uses scripted intent detection, solely for offline QA.
"""
import json
import os
import re
import uuid
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", encoding="utf-8-sig")


class ProviderError(RuntimeError):
    pass


def api_error(exc):
    # SDK errors can include request URLs/keys. Persist only type and status code.
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return ProviderError(f"LLM API failed ({type(exc).__name__}, status={code}). Check key, model, quota and network. No mock fallback.")


class BaseLLMProvider:
    is_mock = False
    provider_name = "base"

    def start_session(self, prompt):
        return [{"role": "user", "content": prompt}]

    def generate_with_tools(self, history, tools_schema, system_prompt=""):
        raise NotImplementedError

    def observe(self, history, results):
        raise NotImplementedError

    def generate(self, prompt, system_prompt=""):
        response = self.generate_with_tools(self.start_session(prompt), [], system_prompt)
        if response["calls"] or not response["content"].strip():
            raise ProviderError("Baseline returned no text.")
        return response["content"]


class MockOfflineProvider(BaseLLMProvider):
    """Small deterministic simulator; it does not demonstrate real LLM reasoning."""
    is_mock = True
    provider_name = "mock"
    model_name = "offline-scripted-lab-v1"

    def generate(self, prompt, system_prompt=""):
        return "[MOCK] Tôi có thể hướng dẫn chung về tín chỉ, GPA và cố vấn; tôi không có tool để tra cứu hồ sơ hoặc đặt lịch. Cần xem quy chế chính thức để biết điều kiện cụ thể."

    def generate_with_tools(self, history, tools_schema, system_prompt=""):
        if isinstance(history, str):
            history = self.start_session(history)
        query = history[0]["content"]
        lower = query.lower()
        observations = [entry for entry in history if entry.get("role") == "tool"]
        student_match = re.search(r"\bSV\d+\b", query, re.IGNORECASE)
        time_match = re.search(r"\b(\d{1,2}:\d{2})\b", query)
        date_match = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", query)
        booking = "đặt lịch" in lower
        calls, content = [], ""

        def call(name, arguments):
            calls.append({"id": "mock_" + uuid.uuid4().hex[:12], "name": name, "arguments": arguments})

        if observations:
            last = observations[-1]
            result = last["observation"]
            if result.get("status") != "SUCCESS":
                content = "[MOCK] " + result.get("message", "Công cụ báo lỗi; chưa thể hoàn thành yêu cầu.")
            elif last["name"] == "academic_query" and booking:
                if time_match and date_match:
                    call("schedule_appointment", {"student_id": result["student_id"],
                         "datetime_str": f"{time_match[1]} {date_match[1]}", "advisor_name": result["data"]["advisor"]})
                else:
                    content = "[MOCK] Bạn muốn đặt lịch vào ngày và giờ nào?"
            elif last["name"] == "schedule_appointment":
                content = "[MOCK] " + result["message"] + " Mã đặt lịch: " + result["booking_id"]
                for previous in observations:
                    if previous["name"] == "academic_query" and previous["observation"].get("data"):
                        data = previous["observation"]["data"]
                        content += f" Hồ sơ mô phỏng: {data['full_name']}, GPA {data['gpa']}."
            else:
                data = result["data"]
                content = f"[MOCK] Hồ sơ mô phỏng {result['student_id']}: {data['full_name']}, GPA {data['gpa']}, cố vấn {data['advisor']}."
        elif ("tra cứu" in lower or booking) and not student_match:
            content = "[MOCK] Bạn vui lòng cung cấp mã sinh viên."
        elif booking and not (time_match and date_match):
            content = "[MOCK] Bạn vui lòng cung cấp đầy đủ ngày và giờ hẹn."
        elif student_match:
            # Only recognize a name explicitly supplied in the demo prompt;
            # the multi-step route always obtains advisor from the actual tool result.
            advisor_match = re.search(r"với\s+(.+?)\s+(?:vào|lúc)", query, re.IGNORECASE)
            if booking and advisor_match and "tra cứu" not in lower:
                call("schedule_appointment", {"student_id": student_match[0].upper(),
                     "datetime_str": f"{time_match[1]} {date_match[1]}", "advisor_name": advisor_match[1].strip()})
            else:
                call("academic_query", {"student_id": student_match[0].upper()})
        else:
            content = "[MOCK] Sinh viên cần theo dõi tín chỉ, GPA, điều kiện tiên quyết và liên hệ cố vấn. Bài lab chưa có văn bản quy chế chính thức của VinUni để khẳng định ngưỡng cụ thể."
        history.append({"role": "assistant", "content": content, "calls": calls})
        return {"calls": calls, "content": content, "usage": {}}

    def observe(self, history, results):
        history.extend({"role": "tool", **result} for result in results)


class OpenAIProvider(BaseLLMProvider):
    provider_name = "openai"

    def __init__(self, api_key=None, model=None, client=None):
        self.model_name = model or os.getenv("LLM_MODEL") or "gpt-4o-mini"
        if client is not None:
            self.client = client
        else:
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"), timeout=45.0, max_retries=1)

    def generate_with_tools(self, history, tools_schema, system_prompt=""):
        if isinstance(history, str):
            history = self.start_session(history)
        kwargs = {"model": self.model_name, "instructions": system_prompt, "input": history}
        if tools_schema:
            kwargs["tools"] = [{"type": "function", **tool, "strict": True} for tool in tools_schema]
        try:
            response = self.client.responses.create(**kwargs)
            if getattr(response, "status", "completed") != "completed":
                raise ProviderError("OpenAI response was incomplete; no final answer accepted.")
            history.extend(response.output)
            calls = [{"id": item.call_id, "name": item.name, "arguments": item.arguments}
                     for item in response.output if item.type == "function_call"]
            usage = response.usage.model_dump(exclude_none=True) if response.usage else {}
            return {"calls": calls, "content": response.output_text or "", "usage": usage,
                    "response_id": response.id}
        except ProviderError:
            raise
        except Exception as exc:
            raise api_error(exc) from None

    def observe(self, history, results):
        history.extend({"type": "function_call_output", "call_id": result["id"],
                        "output": json.dumps(result["observation"], ensure_ascii=False)} for result in results)


class GeminiProvider(BaseLLMProvider):
    provider_name = "gemini"

    def __init__(self, api_key=None, model=None, client=None):
        self.model_name = model or os.getenv("LLM_MODEL") or "gemini-2.5-flash"
        if client is not None:
            self.client = client
        else:
            from google import genai
            from google.genai import types
            self.client = genai.Client(api_key=api_key or os.getenv("GEMINI_API_KEY"),
                                       http_options=types.HttpOptions(timeout=45000))

    def start_session(self, prompt):
        from google.genai import types
        return [types.Content(role="user", parts=[types.Part(text=prompt)])]

    def generate_with_tools(self, history, tools_schema, system_prompt=""):
        from google.genai import types
        if isinstance(history, str):
            history = self.start_session(history)
        config = types.GenerateContentConfig(
            system_instruction=system_prompt, temperature=0.1,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=tool["name"], description=tool["description"],
                                          parameters_json_schema=tool["parameters"])
                for tool in tools_schema
            ])] if tools_schema else None,
        )
        try:
            response = self.client.models.generate_content(model=self.model_name, contents=history, config=config)
            if not response.candidates or not response.candidates[0].content:
                raise ProviderError("Gemini returned no candidate (blocked or empty response).")
            candidate = response.candidates[0]
            if candidate.finish_reason and str(candidate.finish_reason).split(".")[-1] != "STOP":
                raise ProviderError("Gemini did not complete its response; no final answer accepted.")
            model_content = candidate.content
            history.append(model_content)  # Keep parts/signatures intact; do not rebuild from function_calls.
            calls, texts = [], []
            for part in model_content.parts or []:
                if part.function_call:
                    fc = part.function_call
                    calls.append({"id": fc.id or "gemini_" + uuid.uuid4().hex,
                                  "native_id": fc.id, "name": fc.name, "arguments": dict(fc.args or {})})
                elif part.text and not part.thought:
                    texts.append(part.text)
            usage = response.usage_metadata.model_dump(exclude_none=True) if response.usage_metadata else {}
            return {"calls": calls, "content": "\n".join(texts), "usage": usage,
                    "response_id": response.response_id}
        except ProviderError:
            raise
        except Exception as exc:
            raise api_error(exc) from None

    def observe(self, history, results):
        from google.genai import types
        history.append(types.Content(role="user", parts=[
            types.Part(function_response=types.FunctionResponse(
                name=result["name"], id=result.get("native_id"), response=result["observation"]))
            for result in results
        ]))


def get_llm_provider(provider_type=None):
    provider_type = (provider_type or os.getenv("LLM_PROVIDER", "mock")).strip().lower()
    if provider_type == "mock":
        return MockOfflineProvider()
    choices = {"gemini": ("GEMINI_API_KEY", GeminiProvider), "openai": ("OPENAI_API_KEY", OpenAIProvider)}
    if provider_type not in choices:
        raise ProviderError("LLM_PROVIDER must be mock, gemini or openai.")
    key_name, provider_class = choices[provider_type]
    key = os.getenv(key_name, "").strip()
    if not key or key.startswith("your_"):
        raise ProviderError(f"Missing {key_name}. Configure .env or explicitly choose --provider mock for offline tests.")
    return provider_class(api_key=key)
