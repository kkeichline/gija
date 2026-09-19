"""Research launcher: the one API every front door uses (MCP over streamable HTTP).

Four human-shaped tools: list_problems, start_research, research_status, decide.
The low-level tools (submit_udf, run_udf) stay with the harness; people never see them.

Callers are front doors (LibreChat, Open WebUI, ZeroClaw, OpenClaw, the CLI), each with
its own bearer token. A front door vouches for the signed-in person by sending
X-User (email or name); Cedar then decides per person. The launcher holds the only
rights to start harness pods, via Temporal.
"""

import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import cedarpy
import yaml
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError

from launcher.workflows import MODES, TASK_QUEUE, ResearchRequest, ResearchRun, ResearchState

PLATFORM = Path(os.environ.get("PLATFORM_DIR", "/platform"))
CATALOG = yaml.safe_load((PLATFORM / "catalog.yaml").read_text())
CONTRACTS = {c["problem"]: c for c in (yaml.safe_load(p.read_text())
                                       for p in sorted((PLATFORM / "contracts").glob("*.yaml")))}
POLICIES = (PLATFORM / "policies" / "launcher.cedar").read_text()
POLICY_IDS = re.findall(r'@id\("([^"]+)"\)', POLICIES)
FRONT_DOORS: dict[str, str] = json.loads(os.environ.get("FRONTDOOR_TOKENS", "{}"))  # token -> front door name
TEMPORAL = os.environ.get("TEMPORAL_ADDRESS", "temporal.temporal.svc.cluster.local:7233")
RESEARCH_ID = re.compile(r"^research-[0-9a-f]{12}$")

log = logging.getLogger("launcher")
logging.basicConfig(level=logging.INFO, format="%(message)s")
_temporal: Client | None = None


def audit(**event) -> None:
    event["ts"] = round(time.time(), 3)
    log.info(json.dumps(event, default=str))


async def temporal() -> Client:
    global _temporal
    if _temporal is None:
        _temporal = await Client.connect(TEMPORAL)
    return _temporal


# --- who is asking ------------------------------------------------------------------

def caller(ctx: Context) -> tuple[str, str]:
    headers = ctx.headers or {}
    front_door = FRONT_DOORS.get(headers.get("authorization", "").removeprefix("Bearer ").strip())
    if front_door is None:
        audit(event="authn", decision="DENY", reason="unknown front-door token")
        raise ToolError("unauthenticated")
    user = re.sub(r"[^\w.@+-]", "", headers.get("x-user", "").strip().lower())[:128]
    return front_door, user or f"{front_door}:anonymous"


def person(user: str) -> dict:
    p = CATALOG["people"].get(user) or CATALOG["people"]["*"]
    return {"uid": {"type": "User", "id": user}, "attrs": {"team": p["team"], "roles": p["roles"]}, "parents": []}


def authorize(front_door: str, user: str, action: str, resource: dict) -> None:
    entities = [person(user), resource]
    request = {"principal": f'User::"{user}"', "action": f'Action::"{action}"',
               "resource": f'{resource["uid"]["type"]}::"{resource["uid"]["id"]}"', "context": {}}
    requester = resource["attrs"].get("requested_by", {}).get("__entity", {}).get("id")
    if requester and requester != user:
        entities.append(person(requester))
    result = cedarpy.is_authorized(request, POLICIES, entities)
    fired = [POLICY_IDS[int(r[6:])] if r.startswith("policy") and r[6:].isdigit() else r
             for r in result.diagnostics.reasons]
    audit(event="authz", front_door=front_door, user=user, action=action,
          resource=f'{resource["uid"]["type"]}::{resource["uid"]["id"]}',
          decision="ALLOW" if result.allowed else "DENY", policies=fired)
    if not result.allowed:
        why = f"forbidden by {', '.join(fired)}" if fired else "no policy permits it"
        raise ToolError(f"DENIED: {action} ({why})")


def problem_entity(problem: str) -> dict:
    return {"uid": {"type": "Problem", "id": problem}, "attrs": {"teams": CONTRACTS[problem]["teams"]}, "parents": []}


def research_entity(research_id: str, req: dict) -> dict:
    return {"uid": {"type": "Research", "id": research_id},
            "attrs": {"requested_by": {"__entity": {"type": "User", "id": req["requested_by"]}},
                      "teams": CONTRACTS.get(req["problem"], {}).get("teams", [])}, "parents": []}


async def load(research_id: str):
    if not RESEARCH_ID.match(research_id):
        raise ToolError("unknown research id")
    handle = (await temporal()).get_workflow_handle_for(ResearchRun.run, research_id)  # typed result
    try:
        desc = await handle.describe()
    except RPCError:
        raise ToolError("unknown research id")
    return handle, desc, await desc.memo_value("request", default={})


def fabricated_counts(csv_text: str) -> bool:
    rows = [r.split(",") for r in csv_text.strip().splitlines()[1:]]
    return len(rows) >= 5 and len({r[1] for r in rows if len(r) > 1}) == 1


# --- tools ----------------------------------------------------------------------------

mcp = MCPServer(
    name="gija-research",
    instructions=(
        "Ask governed research questions. Call list_problems to see what can be asked, "
        "start_research to begin (it runs in the background, usually several minutes), "
        "research_status to follow it and read the released result, and decide to approve "
        "or reject promoting the UDF (approvers only, never your own request)."
    ),
)


@mcp.tool()
async def list_problems(ctx: Context) -> dict:
    """List the research problems you can ask about, with their questions and what a result looks like."""
    front_door, user = caller(ctx)
    authorize(front_door, user, "list_problems", {"uid": {"type": "Catalog", "id": "all"}, "attrs": {}, "parents": []})
    team = person(user)["attrs"]["team"]
    return {"you": user, "problems": [
        {"problem": p, "question": c["question"].strip(),
         "result_columns": [col["name"] for col in c["artifact"]["columns"]],
         "privacy_rule": f"groups smaller than {c['artifact']['min_group_size']['k']} are suppressed"}
        for p, c in CONTRACTS.items() if team in c["teams"]]}


@mcp.tool()
async def start_research(problem: str, question: str, ctx: Context) -> dict:
    """Start a research run on a problem. Returns a research_id; it runs in the background."""
    front_door, user = caller(ctx)
    if problem not in CONTRACTS:
        raise ToolError(f"unknown problem {problem!r}; call list_problems")
    if not 3 <= len(question) <= 2000:
        raise ToolError("question must be 3-2000 characters")
    authorize(front_door, user, "start_research", problem_entity(problem))
    research_id = f"research-{uuid.uuid4().hex[:12]}"
    c = CONTRACTS[problem]
    loop, approval = c.get("loop", {}), c.get("approval", {})
    mode = approval.get("mode", "auto_if_clean")
    if mode not in MODES:
        raise ToolError(f"problem {problem!r} has an invalid approval.mode {mode!r}")
    req = ResearchRequest(problem=problem, question=question, requested_by=user, front_door=front_door,
                          max_sessions=int(loop.get("max_sessions", 4)), max_hours=float(loop.get("max_hours", 10)),
                          approval_mode=mode, reviewer=bool(approval.get("reviewer", True)))
    await (await temporal()).start_workflow(
        ResearchRun.run, req, id=research_id, task_queue=TASK_QUEUE,
        memo={"request": asdict(req)}, execution_timeout=timedelta(days=7, hours=req.max_hours + 2))
    audit(event="research_started", research_id=research_id, front_door=front_door, user=user, problem=problem)
    return {"research_id": research_id, "status": "started",
            "next": "call research_status with this id in a few minutes"}


@mcp.tool()
async def research_status(research_id: str, ctx: Context) -> dict:
    """Get a research run's progress and, once released, its result table."""
    front_door, user = caller(ctx)
    handle, desc, req = await load(research_id)
    authorize(front_door, user, "research_status", research_entity(research_id, req))
    if desc.status == WorkflowExecutionStatus.RUNNING:
        state = await handle.query(ResearchRun.status)
    else:
        state = await handle.result() if desc.status == WorkflowExecutionStatus.COMPLETED else None
    if state is None:
        return {"research_id": research_id, "status": desc.status.name.lower()}
    out = {"research_id": research_id, "problem": req.get("problem"), "question": req.get("question"),
           "requested_by": req.get("requested_by"), "status": state.status,
           "approval_mode": req.get("approval_mode"), "sessions": len(state.sessions),
           "runs": [{"run_id": r.run_id, "outcome": r.status, "notes": r.notes, "checks": r.checks}
                    for r in state.runs]}
    if state.review:
        out["review"] = {"risk": state.review.risk, "answers_the_question": state.review.answers_the_question,
                         "flags": state.review.flags, "rationale": state.review.rationale}
    if state.escalation:
        out["needs_a_person_because"] = state.escalation
    released = next((r for r in state.runs if r.run_id == state.released_run), None)
    if released:
        out["released_run"] = released.run_id
        out["result_csv"] = released.artifact_csv  # from the gateway, not from the agent
        if fabricated_counts(released.artifact_csv):
            out["warning"] = ("Every row has the same group size; counts were probably written by the UDF, "
                              "so small-group suppression may not have applied. Review before approving.")
        out["agent_summary"] = state.answers[-1] if state.answers else ""
    if state.decided_by:
        out["decided_by"] = state.decided_by
    return out


@mcp.tool()
async def decide(research_id: str, decision: str, ctx: Context) -> dict:
    """Approve or reject promoting a research run's UDF. decision is 'approve' or 'reject'. Approvers only."""
    front_door, user = caller(ctx)
    if decision not in ("approve", "reject"):
        raise ToolError("decision must be 'approve' or 'reject'")
    handle, desc, req = await load(research_id)
    authorize(front_door, user, "decide", research_entity(research_id, req))
    state = await handle.query(ResearchRun.status) if desc.status == WorkflowExecutionStatus.RUNNING else None
    if state is None or state.status != "awaiting approval":
        raise ToolError("this research is not awaiting approval")
    await handle.signal(ResearchRun.decide, args=[decision, user])
    audit(event="research_decided", research_id=research_id, user=user, decision=decision)
    return {"research_id": research_id, "decision": decision, "by": user}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


app = mcp.streamable_http_app(
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
