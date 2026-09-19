"""Smoke test: plays the agent's role against the governed gateway, with no model.

`make smoke` runs this as a pod in `agents` with the harness label and token, so it
takes the same network path and holds the same credentials as a Claude Code pod.
Each step prints what the gateway decided.
"""

import asyncio
import json
import os
import time

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

URL = os.environ.get("GATEWAY_URL", "http://mcp-gateway.gateway.svc.cluster.local:8080/mcp")
PROBLEM = "claims_cost_drivers"

GOOD = '''
import pandas as pd

def run(df):
    keys = ["age_band", "plan", "chronic_conditions"]
    out = (df.groupby(keys)
             .agg(n_members=("claim_cost", "size"), avg_claim_cost=("claim_cost", "mean"))
             .reset_index())
    out["avg_claim_cost"] = out["avg_claim_cost"].round(2)
    return out.sort_values("avg_claim_cost", ascending=False)[keys + ["n_members", "avg_claim_cost"]]
'''

LEAKY = '''
import pandas as pd

def run(df):
    # Tries to smuggle individual members' costs out, dressed up as one-member groups.
    rows = df.head(50)
    return pd.DataFrame({"age_band": rows["age_band"], "plan": rows["plan"],
                         "chronic_conditions": rows["chronic_conditions"], "n_members": 1,
                         "avg_claim_cost": rows["claim_cost"]})
'''

SNEAKY_NETWORK = '''
def run(df):
    # Dodges the import lint, then tries to phone home. The job's network isolation stops it.
    urlopen = __import__("urllib.request", fromlist=["urlopen"]).urlopen
    urlopen("https://example.com", timeout=5)
    return df
'''

BAD_IMPORT = '''
import socket

def run(df):
    return df
'''


def payload(result) -> dict:
    data = result.structured_content or json.loads(result.content[0].text)
    return data["result"] if set(data) == {"result"} else data


async def call(s: ClientSession, step: str, tool: str, **args) -> dict | None:
    result = await s.call_tool(tool, args, read_timeout_seconds=200)
    if result.is_error:
        print(f"  ✗ {step}\n      {' '.join(getattr(c, 'text', '') for c in result.content)}")
        return None
    data = payload(result)
    summary = {k: data[k] for k in ("udf_id", "run_id", "status", "rows", "notes") if k in data}
    print(f"  ✓ {step}\n      {json.dumps(summary)}")
    if data.get("artifact_csv"):
        for line in data["artifact_csv"].splitlines()[:4]:
            print(f"        {line}")
    return data


async def run_udf(s, step, code, dataset="claims_synthetic"):
    staged = await call(s, f"{step}: submit_udf", "submit_udf", problem=PROBLEM, code=code)
    if staged:
        return await call(s, f"{step}: run_udf on {dataset}", "run_udf",
                          problem=PROBLEM, udf_id=staged["udf_id"], dataset=dataset)


async def connect(headers: dict):
    http = create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(200.0))
    return http, streamable_http_client(URL, http_client=http)


async def main() -> None:
    session_id = f"smoke-{int(time.time())}"
    print(f"Gateway: {URL}\nSession: {session_id}\n")

    print("1. No token")
    async with create_mcp_http_client(timeout=httpx2.Timeout(30.0)) as http, \
            streamable_http_client(URL, http_client=http) as streams, \
            ClientSession(streams[0], streams[1]) as s:
        await s.initialize()
        await call(s, "list_problems without a bearer token", "list_problems")

    headers = {"Authorization": f"Bearer {os.environ['AGENT_TOKEN']}", "X-Session-Id": session_id}
    async with create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(200.0)) as http, \
            streamable_http_client(URL, http_client=http) as streams, \
            ClientSession(streams[0], streams[1]) as s:
        await s.initialize()

        print("\n2. Discovery")
        problems = await call(s, "list_problems", "list_problems")
        for p in (problems or {}).get("problems", []):
            print(f"      {p['problem']}: runs_remaining={p['runs_remaining']}")
            for name, d in p["datasets"].items():
                print(f"        {name} ({d['sensitivity']}): {d['columns']}")

        print("\n3. The happy path")
        await run_udf(s, "aggregate by segment", GOOD)

        print("\n4. Policy: PII is forbidden even though a grant exists")
        staged = await call(s, "submit_udf", "submit_udf", problem=PROBLEM, code=GOOD)
        await call(s, "run_udf on members_pii", "run_udf", problem=PROBLEM, udf_id=staged["udf_id"], dataset="members_pii")

        print("\n5. The import lint (fast feedback, not the boundary)")
        await call(s, "submit_udf importing socket", "submit_udf", problem=PROBLEM, code=BAD_IMPORT)

        print("\n6. The boundary: a UDF that dodges the lint and tries the network")
        await run_udf(s, "sneaky network UDF", SNEAKY_NETWORK)

        print("\n7. The release rule: a UDF that tries to leak row-level values (claimed counts are checked)")
        await run_udf(s, "leaky UDF", LEAKY)

        print("\n8. The iteration bound (5 runs per session; 3 used so far)")
        for i in (4, 5, 6):
            await call(s, f"run #{i}", "run_udf", problem=PROBLEM, udf_id=staged["udf_id"], dataset="claims_synthetic")

        print("\n9. Another run id")
        await call(s, "get_run on a run this agent doesn't own", "get_run", run_id="run-0000000000")

    print("\nDone. `make audit` shows each decision; the notifier woke OpenClaw once per finished run.")


if __name__ == "__main__":
    asyncio.run(main())
