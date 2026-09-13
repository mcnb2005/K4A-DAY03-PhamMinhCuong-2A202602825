"""In-process MCP teaching simulator using JSON-RPC 2.0 envelopes.

This is the starter's local transport, not a full MCP SDK/HTTP/stdio server.
The dispatcher supports tools/list and tools/call for the lab client.
"""
import copy
import itertools
import json
import sys
from tools import TOOLS_SCHEMA, dispatch_tool_call


class MCPAcademicServer:
    def __init__(self, server_name="vinuni-academic-mcp-server"):
        self.server_name = server_name
        self.version = "2026.1.0"
        self._request_ids = itertools.count(1)
        self.bookings = {}

    def list_tools(self):
        return copy.deepcopy(TOOLS_SCHEMA)

    def handle_request(self, request):
        request_id = request.get("id") if isinstance(request, dict) else None
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
                or isinstance(request_id, bool) or not isinstance(request_id, (int, str))):
            return self._error(None, -32600, "Invalid Request: a request id is required by this lab simulator")
        method = request["method"]
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid params")
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["parameters"]}
                for t in self.list_tools()
            ]}}
        if method != "tools/call":
            return self._error(request_id, -32601, "Method not found")
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not isinstance(params.get("arguments"), dict):
            return self._error(request_id, -32602, "Tool name and arguments object are required")
        content = json.loads(dispatch_tool_call(tool_name, params["arguments"], bookings=self.bookings))
        # Lab-compatible result payload. Application/tool errors are observations.
        return {"jsonrpc": "2.0", "id": request_id, "server": self.server_name, "tool": tool_name, "result": content}

    @staticmethod
    def _error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def call_tool(self, tool_name, arguments):
        # JSON round trip makes the simulated client/server boundary explicit.
        request = {"jsonrpc": "2.0", "id": next(self._request_ids), "method": "tools/call",
                   "params": {"name": tool_name, "arguments": arguments}}
        return self.handle_request(json.loads(json.dumps(request, ensure_ascii=False)))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    server = MCPAcademicServer()
    print(f"[MCP SERVER] {server.server_name} (Version: {server.version})")
    print(f"Tools: {len(server.list_tools())}; transport: in-process simulation")
    print(json.dumps(server.call_tool("academic_query", {"student_id": "SV2026001"}), ensure_ascii=False, indent=2))
