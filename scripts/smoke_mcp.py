"""Smoke-Test: startet den MCP-Server über stdio und spricht echtes JSON-RPC.

Aufruf:
    uv run python scripts/smoke_mcp.py --root examples/demo-gdd
"""

from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pump(stream, sink: queue.Queue) -> None:
    try:
        for line in stream:
            sink.put(line)
    finally:
        stream.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()

    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = ROOT / ".venv" / "bin" / "python"

    proc = subprocess.Popen(
        [str(python), "-m", "atlas", "serve", "--root", args.root],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True).start()

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "atlas_where", "arguments": {"topic": "Wo finde ich das Kampfsystem?"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "atlas_where", "arguments": {"topic": "Rüstung"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "atlas_outline", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "atlas_tasks", "arguments": {"status": "open"}}},
    ]

    for request in requests:
        proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()

    pending = {1, 2, 3, 4, 5, 6}
    failures = 0
    deadline = time.time() + 30

    while pending and time.time() < deadline:
        try:
            line = lines.get(timeout=1.0)
        except queue.Empty:
            continue
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        message_id = message.get("id")
        if message_id not in pending:
            continue
        pending.discard(message_id)

        if "error" in message:
            failures += 1
            print(f"[FEHLER] id={message_id}: {message['error']}")
            continue

        result = message["result"]
        if message_id == 1:
            info = result["serverInfo"]
            print(f"[ok] initialize  -> {info['name']} {info['version']}, Protokoll {result['protocolVersion']}")
            # Die Instructions sind die einzige Regel, die ein Client ohne
            # geladene AGENTS.md sieht — sie müssen im Handshake ankommen.
            instructions = result.get("instructions") or ""
            missing = [needle for needle in
                       (".atlas/cache", "grünen Tests", "atlas_set_status", "row_key")
                       if needle not in instructions]
            if missing:
                failures += 1
                print(f"[FEHLER] instructions unvollständig, fehlt: {missing}")
            else:
                print(f"[ok] instructions -> {len(instructions)} Zeichen, Guardrails im Handshake")
        elif message_id == 2:
            names = [tool["name"] for tool in result["tools"]]
            print(f"[ok] tools/list  -> {len(names)} Tools: {', '.join(names)}")
        elif message_id in (3, 4):
            text = result["content"][0]["text"]
            data = json.loads(text)
            label = "Kampfsystem" if message_id == 3 else "Dialogtool"
            if data.get("found"):
                print(f"[ok] where({label}) -> {data['path']}:{data['line']} § {data['heading']}")
                print(f"     {len(text)} Zeichen (~{len(text)//4} Tokens), Anker {data['anchor']}")
            else:
                failures += 1
                print(f"[FEHLER] where({label}) fand nichts")
        elif message_id == 5:
            data = json.loads(result["content"][0]["text"])
            print(f"[ok] atlas_outline -> {len(data.get('outline',''))} Zeichen, truncated={data.get('truncated')}")
        elif message_id == 6:
            data = json.loads(result["content"][0]["text"])
            print(f"[ok] atlas_tasks  -> {data['count']} offene Tasks")

    if pending:
        failures += 1
        print(f"[FEHLER] keine Antwort für ids: {sorted(pending)}")

    proc.terminate()
    stderr = proc.stderr.read() if proc.stderr else ""
    if stderr.strip():
        print("\n--- server stderr ---")
        print(stderr.strip()[:1200])

    print(f"\nFehler: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
