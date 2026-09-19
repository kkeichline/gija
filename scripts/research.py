"""The simplest front door: ask, follow, and decide on research from the terminal.

    make ask Q="Which segments cost the most?"              [AS=you@example.com]
    make status ID=research-…                               [AS=…]
    make decide ID=research-… DECISION=approve AS=approver@example.com

It uses the launcher's MCP tools with the `cli` front-door token, through a temporary
kubectl port-forward, exactly as LibreChat, Open WebUI, or ZeroClaw will.
"""

import argparse
import asyncio
import base64
import contextlib
import json
import socket
import subprocess
import sys
import time

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

KUBECTL = ["kubectl", "--context", "kind-openclaw"]


def cli_token() -> str:
    raw = subprocess.run([*KUBECTL, "get", "secret", "launcher-secrets", "-n", "launcher", "-o",
                          "jsonpath={.data.FRONTDOOR_TOKENS}"], capture_output=True, text=True, check=True).stdout
    tokens = json.loads(base64.b64decode(raw))
    return next(t for t, door in tokens.items() if door == "cli")


@contextlib.contextmanager
def port_forward():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([*KUBECTL, "port-forward", "-n", "launcher", "svc/launcher", f"{port}:8080"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.2):
                break
            time.sleep(0.2)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()


async def call(url: str, user: str, tool: str, **args) -> dict:
    headers = {"Authorization": f"Bearer {cli_token()}", "X-User": user}
    async with create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(60.0)) as http, \
            streamable_http_client(url, http_client=http) as streams, \
            ClientSession(streams[0], streams[1]) as session:
        await session.initialize()
        result = await session.call_tool(tool, args)
        if result.is_error:
            sys.exit(" ".join(getattr(c, "text", "") for c in result.content))
        data = result.structured_content or json.loads(result.content[0].text)
        return data["result"] if set(data) == {"result"} else data


def show(data: dict) -> None:
    csv = data.pop("result_csv", "")
    summary = data.pop("agent_summary", "")
    print(json.dumps(data, indent=2))
    if csv:
        print("\nReleased result (first 12 rows):")
        print("\n".join(csv.strip().splitlines()[:13]))
    if summary:
        print(f"\nAgent's summary (untrusted narrative):\n{summary.strip()[:1500]}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["problems", "ask", "status", "decide", "watch"])
    ap.add_argument("--as", dest="user", default="researcher@example.com")
    ap.add_argument("--problem", default="claims_cost_drivers")
    ap.add_argument("--question", default="")
    ap.add_argument("--id", default="")
    ap.add_argument("--decision", default="approve")
    a = ap.parse_args()
    with port_forward() as url:
        if a.command == "problems":
            show(await call(url, a.user, "list_problems"))
        elif a.command == "ask":
            show(await call(url, a.user, "start_research", problem=a.problem, question=a.question))
        elif a.command == "status":
            show(await call(url, a.user, "research_status", research_id=a.id))
        elif a.command == "decide":
            show(await call(url, a.user, "decide", research_id=a.id, decision=a.decision))
        elif a.command == "watch":
            while True:
                data = await call(url, a.user, "research_status", research_id=a.id)
                p = data.get("progress", {})
                line = [data["status"]]
                if p:
                    line += [f"{p.get('elapsed', '?')} elapsed", f"{p.get('tool_calls', 0)} tool calls"]
                    if p.get("last_step"):
                        line.append(f"last: {p['last_step']} ({p['last_step_ago']} ago)")
                    if p.get("looks_stalled"):
                        line.append("LOOKS STALLED")
                    if p.get("detail"):
                        line.append(p["detail"])
                print(time.strftime("%H:%M:%S"), " · ".join(line), flush=True)
                if not data["status"].startswith(("researching", "starting", "reviewing")):
                    show(data)
                    return
                await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(main())
