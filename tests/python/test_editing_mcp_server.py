import io
import json
from pathlib import Path
import tempfile
import unittest

from clipen.editing import ToolResult
from clipen.mcp_editing import EditingMcpServer, run_stdio
from clipen.projects import ProjectError


class FakeTools:
    def __init__(self, image=None):
        self.activated = 0
        self.calls = []
        self.image = image

    def activate(self):
        self.activated += 1

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "blocked":
            raise ProjectError("tool_not_allowed", "blocked", status_code=404)
        images = () if self.image is None else (self.image,)
        return ToolResult({"ok": True}, images)


class EditingMcpServerTests(unittest.TestCase):
    def test_lists_only_bounded_editing_tools(self):
        server = EditingMcpServer(FakeTools())
        response = server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        names = {tool["name"] for tool in response["result"]["tools"]}

        self.assertEqual(
            names,
            {
                "get_edit_status",
                "search_media",
                "inspect_clip",
                "inspect_sequence",
                "replace_sequence",
                "add_title",
                "set_clip_effect",
                "undo_revision",
                "finish_edit",
            },
        )
        self.assertNotIn("shell", json.dumps(response))
        self.assertNotIn("filesystem", json.dumps(response))

    def test_stdio_initializes_calls_tool_and_embeds_local_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "frame.png"
            image.write_bytes(b"png-data")
            tools = FakeTools(image)
            source = io.BytesIO(
                b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
                b'{"name":"inspect_clip","arguments":{"candidate_id":"candidate-a"}}}\n'
            )
            destination = io.BytesIO()

            self.assertEqual(run_stdio(EditingMcpServer(tools), source, destination), 0)
            messages = [json.loads(line) for line in destination.getvalue().splitlines()]

        self.assertEqual(tools.activated, 1)
        self.assertEqual(messages[0]["result"]["serverInfo"]["name"], "clipen-editing")
        content = messages[1]["result"]["content"]
        self.assertEqual([item["type"] for item in content], ["text", "image"])
        self.assertEqual(content[1]["mimeType"], "image/png")

    def test_tool_error_is_a_tool_result_not_a_server_crash(self):
        response = EditingMcpServer(FakeTools()).handle(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "blocked", "arguments": {}},
            }
        )
        self.assertTrue(response["result"]["isError"])
        self.assertIn("tool_not_allowed", response["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
