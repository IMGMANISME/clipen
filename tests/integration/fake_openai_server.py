"""A minimal fake OpenAI-compatible /v1/chat/completions server, used to
exercise the real OpenAICompatibleClient code path (the same one Ollama
uses) at zero cost. Branches on request content:

- a vision prompt (unique marker phrase from analysis/visual.py's
  prompt) gets a canned short description back as plain text;
- anything else is treated as the narrative-assembly prompt (unique
  marker phrase from editing/narrative.py's system prompt) and gets a
  JSON array reply built from the candidate filenames found in the
  prompt's per-clip "### filename" headers, reversed and with the last
  one dropped -- gives tests something concrete to assert reordering and
  exclusion against.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeOpenAI/0.1"

    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.call_log.append(body)  # type: ignore[attr-defined]

        messages = body.get("messages", [])
        full_text = " ".join(_text_of(m.get("content", "")) for m in messages)

        if "frames from one raw video clip" in full_text:
            reply_text = "A person talks to the camera in a well-lit room; steady shot."
        elif "assembling an ordered vlog edit" in full_text:
            filenames = re.findall(r"###\s+(\S+\.mp4)", full_text)
            ordered = list(reversed(filenames))
            items = []
            for i, name in enumerate(ordered):
                if i == len(ordered) - 1 and len(ordered) > 1:
                    items.append({"file": name, "keep": False, "reason": "fake server: dropped"})
                else:
                    items.append(
                        {
                            "file": name,
                            "keep": True,
                            "start": 0.0,
                            "end": 9999.0,
                            "reason": "fake server: kept",
                        }
                    )
            reply_text = json.dumps(items)
        else:
            reply_text = json.dumps([])

        response = {
            "id": "fake-completion",
            "object": "chat.completion",
            "created": 1700000000,
            "model": body.get("model", "fake-model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply_text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 8, "total_tokens": 50},
        }
        payload = json.dumps(response).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FakeOpenAIServer:
    def __init__(self) -> None:
        self._httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.call_log = []  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "FakeOpenAIServer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/v1"

    @property
    def call_count(self) -> int:
        return len(self._httpd.call_log)  # type: ignore[attr-defined]

    @property
    def call_log(self) -> list[dict]:
        return self._httpd.call_log  # type: ignore[attr-defined]
