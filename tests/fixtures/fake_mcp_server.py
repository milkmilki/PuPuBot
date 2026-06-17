import json
import os
from pathlib import Path
import sys

counter_path = os.environ.get("FAKE_MCP_COUNTER_PATH", "").strip()
if counter_path:
    path = Path(counter_path)
    try:
        count = int(path.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        count = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(count + 1), encoding="utf-8")


def read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    if line.lower().startswith(b"content-length:"):
        length = int(line.split(b":", 1)[1].strip())
        while True:
            header = sys.stdin.buffer.readline()
            if not header or not header.strip():
                break
        payload = sys.stdin.buffer.read(length)
    else:
        payload = line.strip()
    return json.loads(payload.decode("utf-8"))


def send_message(message):
    payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if method == "notifications/initialized":
        continue
    request_id = message.get("id")
    if method == "initialize":
        send_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0.1"},
                },
            }
        )
    elif method == "tools/list":
        send_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": "tavily_search",
                            "description": "Fake Tavily search",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                            },
                        }
                    ]
                },
            }
        )
    elif method == "tools/call":
        query = (message.get("params") or {}).get("arguments", {}).get("query", "")
        if query == "__exit__":
            send_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "exiting",
                            }
                        ]
                    },
                }
            )
            sys.exit(0)
        send_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"fake result for {query}",
                        }
                    ]
                },
            }
        )
    else:
        send_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            }
        )
