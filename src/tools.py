"""Two local teaching tools. All records and appointments are simulated lab data."""
import copy
import hashlib
import json
from datetime import datetime

from jsonschema import Draft202012Validator

TOOLS_SCHEMA = [
    {
        "name": "academic_query",
        "description": "Tra cứu hồ sơ học vụ mô phỏng theo mã sinh viên, gồm GPA và cố vấn. Không tìm thấy trả NOT_FOUND.",
        "parameters": {
            "type": "object",
            "properties": {
                "student_id": {"type": "string", "minLength": 1, "description": "Mã sinh viên người dùng cung cấp, ví dụ SV2026001. Không tự đoán."}
            },
            "required": ["student_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "schedule_appointment",
        "description": "Đặt lịch tư vấn MÔ PHỎNG khi người dùng yêu cầu. Cần đủ mã sinh viên, ngày giờ và tên cố vấn; nếu chưa biết cố vấn, tra academic_query trước. Không đặt nếu hồ sơ NOT_FOUND.",
        "parameters": {
            "type": "object",
            "properties": {
                "student_id": {"type": "string", "minLength": 1, "description": "Mã sinh viên cần đặt lịch."},
                "datetime_str": {"type": "string", "minLength": 1, "description": "Giờ địa phương UTC+07:00, định dạng HH:MM DD/MM/YYYY, ví dụ 14:00 15/09/2026."},
                "advisor_name": {"type": "string", "minLength": 1, "description": "Tên cố vấn chính xác từ người dùng hoặc kết quả academic_query."},
            },
            "required": ["student_id", "datetime_str", "advisor_name"],
            "additionalProperties": False,
        },
    },
]

# Dữ liệu hư cấu từ starter; không phải hồ sơ học vụ thật của VinUni.
MOCK_DATABASE = {
    "SV2026001": {
        "full_name": "Nguyễn Văn An", "class": "AI-K4", "gpa": 3.85,
        "email": "an.nv@vinuni.edu.vn", "status": "Đang học", "advisor": "PGS.TS Nguyễn Văn A",
    },
    "SV2026002": {
        "full_name": "Trần Thị Bình", "class": "AI-K4", "gpa": 3.60,
        "email": "binh.tt@vinuni.edu.vn", "status": "Đang học", "advisor": "TS. Lê Thị B",
    },
}


def execute_academic_query(student_id: str) -> str:
    student_id = student_id.strip().upper()
    student = MOCK_DATABASE.get(student_id)
    if student is None:
        result = {"status": "NOT_FOUND", "student_id": student_id, "message": f"Không tìm thấy sinh viên {student_id} trong dữ liệu mô phỏng."}
    else:
        result = {"status": "SUCCESS", "student_id": student_id, "data": copy.deepcopy(student)}
    return json.dumps({"simulated": True, **result}, ensure_ascii=False)


def execute_schedule_appointment(student_id: str, datetime_str: str, advisor_name: str, *, bookings=None) -> str:
    """Idempotent in-memory booking; each MCP server owns its own booking store."""
    student_id = student_id.strip().upper()
    student = MOCK_DATABASE.get(student_id)
    if student is None:
        return execute_academic_query(student_id)
    try:
        appointment_time = datetime.strptime(datetime_str.strip(), "%H:%M %d/%m/%Y")
    except ValueError:
        return json.dumps({"status": "INVALID_ARGUMENTS", "message": "Ngày giờ không hợp lệ. Dùng HH:MM DD/MM/YYYY."}, ensure_ascii=False)
    if advisor_name.strip().casefold() != student["advisor"].casefold():
        return json.dumps({"status": "ADVISOR_MISMATCH", "message": "Cố vấn không khớp hồ sơ. Hãy tra cứu lại trước khi đặt lịch."}, ensure_ascii=False)
    normalized_time = appointment_time.strftime("%H:%M %d/%m/%Y")
    key = (student_id, normalized_time, student["advisor"])
    store = bookings if bookings is not None else {}
    if key in store:
        return json.dumps({**store[key], "already_booked": True}, ensure_ascii=False)
    if any(slot[1:] == key[1:] and slot[0] != student_id for slot in store):
        return json.dumps({"status": "SLOT_UNAVAILABLE", "message": "Cố vấn đã có lịch tại thời điểm này. Vui lòng chọn giờ khác."}, ensure_ascii=False)
    booking_id = "BK-" + hashlib.sha256("|".join(key).encode()).hexdigest()[:12].upper()
    result = {
        "status": "SUCCESS", "simulated": True, "booking_id": booking_id,
        "student_id": student_id, "datetime": normalized_time, "timezone": "UTC+07:00",
        "advisor": student["advisor"], "already_booked": False,
        "message": f"Đặt lịch mô phỏng thành công cho {student_id} với {student['advisor']} vào {normalized_time} (UTC+07:00).",
    }
    store[key] = result
    return json.dumps(result, ensure_ascii=False)


TOOL_ROUTER = {"academic_query": execute_academic_query, "schedule_appointment": execute_schedule_appointment}
VALIDATORS = {tool["name"]: Draft202012Validator(tool["parameters"]) for tool in TOOLS_SCHEMA}


def dispatch_tool_call(tool_name: str, arguments, *, bookings=None) -> str:
    if tool_name not in TOOL_ROUTER:
        return json.dumps({"status": "UNKNOWN_TOOL", "message": f"Tool {tool_name!r} không tồn tại."}, ensure_ascii=False)
    errors = sorted(VALIDATORS[tool_name].iter_errors(arguments), key=lambda error: str(error.path))
    if errors:
        return json.dumps({"status": "INVALID_ARGUMENTS", "message": errors[0].message}, ensure_ascii=False)
    if any(not value.strip() for value in arguments.values()):
        return json.dumps({"status": "INVALID_ARGUMENTS", "message": "Tham số không được để trống."}, ensure_ascii=False)
    try:
        if tool_name == "schedule_appointment":
            return execute_schedule_appointment(**arguments, bookings=bookings)
        return TOOL_ROUTER[tool_name](**arguments)
    except Exception as exc:
        # Do not expose stack traces, credentials, or arbitrary backend details to the LLM.
        return json.dumps({"status": "EXECUTION_ERROR", "message": f"Tool execution failed ({type(exc).__name__})."})
