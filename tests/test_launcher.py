"""Offline checks for the launcher: front-door policies and the ResearchRun loop.

The workflow runs in Temporal's time-skipping test server with the activities mocked,
so no cluster or model is needed.  Run: make test
"""

import os
import sys
import uuid
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1] / "platform"
os.environ.setdefault("PLATFORM_DIR", str(PLATFORM))
sys.path.insert(0, str(PLATFORM))

import pytest  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from launcher import server  # noqa: E402
from launcher.activities import Review, Run, SessionResult  # noqa: E402
from launcher.workflows import ResearchRequest, ResearchRun  # noqa: E402

APPROVER = "approver@example.com"


def research(requested_by: str) -> dict:
    return server.research_entity("research-000000000000",
                                  {"requested_by": requested_by, "problem": "claims_cost_drivers"})


# --- policies ------------------------------------------------------------------------

def test_anyone_on_the_team_can_ask():
    server.authorize("cli", "someone@example.com", "start_research", server.problem_entity("claims_cost_drivers"))


def test_people_see_their_own_research_but_not_others():
    server.authorize("cli", "a@example.com", "research_status", research("a@example.com"))
    with pytest.raises(ToolError, match="DENIED"):
        server.authorize("cli", "b@example.com", "research_status", research("a@example.com"))


def test_approvers_see_and_decide_team_research():
    server.authorize("cli", APPROVER, "research_status", research("a@example.com"))
    server.authorize("cli", APPROVER, "decide", research("a@example.com"))


def test_non_approvers_cannot_decide():
    with pytest.raises(ToolError, match="no policy permits"):
        server.authorize("cli", "b@example.com", "decide", research("a@example.com"))


def test_nobody_approves_their_own_request():
    with pytest.raises(ToolError, match="no-self-approval"):
        server.authorize("cli", APPROVER, "decide", research(APPROVER))


def test_fabricated_counts_are_flagged():
    fake = "segment,n_members,avg_claim_cost\n" + "".join(f"s{i},100,{i}.0\n" for i in range(6))
    honest = "segment,n_members,avg_claim_cost\n" + "".join(f"s{i},{30 + i},{i}.0\n" for i in range(6))
    assert server.fabricated_counts(fake) and not server.fabricated_counts(honest)


# --- workflow ------------------------------------------------------------------------

def mocked_activities(outcomes: list[str], approvals: list, review: Review | None = None, checks=()):
    sessions = iter(range(100))

    @activity.defn(name="run_harness_session")
    async def run_harness_session(task: str, name: str) -> SessionResult:
        next(sessions)
        return SessionResult(session=name, answer=f"answer to: {task[:20]}", is_error=False)

    @activity.defn(name="fetch_runs")
    async def fetch_runs(session: str) -> list[Run]:
        status = outcomes.pop(0)
        return [Run(run_id=f"run-{session}", status=status, notes=[] if status == "released" else ["bad header"],
                    checks=list(checks) if status == "released" else [])]

    @activity.defn(name="review_run")
    async def review_run(question: str, run: Run) -> Review:
        return review or Review(risk="low", answers_the_question=True)

    @activity.defn(name="approve_run")
    async def approve_run(run_id: str, approved_by: str) -> str:
        approvals.append((run_id, approved_by))
        return "udf-x"

    return [run_harness_session, fetch_runs, review_run, approve_run]


async def run_workflow(outcomes, decision=None, mode="human", review=None, checks=(), max_sessions=2):
    approvals: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="t", workflows=[ResearchRun],
                          activities=mocked_activities(outcomes, approvals, review, checks)):
            handle = await env.client.start_workflow(
                ResearchRun.run, ResearchRequest("claims_cost_drivers", "which segments cost most?", "a@example.com",
                                                 "cli", max_sessions=max_sessions, approval_mode=mode),
                id=f"research-{uuid.uuid4().hex[:12]}", task_queue="t")
            if decision:
                while (await handle.query(ResearchRun.status)).status != "awaiting approval":
                    await env.sleep(1)
                await handle.signal(ResearchRun.decide, args=decision)
            return await handle.result(), approvals


async def test_retries_with_feedback_then_waits_for_approval_and_promotes():
    state, approvals = await run_workflow(["rejected", "released"], decision=["approve", APPROVER])
    assert len(state.sessions) == 2
    assert state.status == "approved: UDF promoted" and state.decided_by == APPROVER
    assert approvals == [(f"run-{state.sessions[-1]}", APPROVER)]


async def test_session_budget_is_bounded():
    state, approvals = await run_workflow(["failed", "rejected"])
    assert len(state.sessions) == 2 and state.status == "finished without a released result" and not approvals


async def test_rejection_promotes_nothing():
    state, approvals = await run_workflow(["released"], decision=["reject", APPROVER])
    assert state.status == "rejected" and not approvals


async def test_unanswered_approval_expires():
    state, approvals = await run_workflow(["released"])  # time skipping jumps past the 7-day wait
    assert state.status == "approval expired" and not approvals


async def test_auto_if_clean_promotes_a_clean_run_without_a_person():
    state, approvals = await run_workflow(["released"], mode="auto_if_clean")
    assert state.status == "approved: UDF promoted automatically"
    assert approvals == [(f"run-{state.sessions[-1]}", "policy:auto_if_clean")] and not state.escalation


async def test_auto_if_clean_escalates_on_a_deterministic_check():
    state, approvals = await run_workflow(["released"], mode="auto_if_clean", checks=["calls __import__()"],
                                          decision=["approve", APPROVER])
    assert state.escalation == ["check: calls __import__()"] and approvals == [(f"run-{state.sessions[-1]}", APPROVER)]


async def test_auto_if_clean_escalates_when_the_reviewer_is_worried():
    worried = Review(risk="medium", answers_the_question=True, flags=["numbers look hard-coded"])
    state, approvals = await run_workflow(["released"], mode="auto_if_clean", review=worried,
                                          decision=["reject", APPROVER])
    assert "reviewer: risk medium" in state.escalation and state.status == "rejected" and not approvals


async def test_an_unusable_review_fails_closed():
    state, _ = await run_workflow(["released"], mode="auto_if_clean",
                                  review=Review(risk="unknown", answers_the_question=False))
    assert state.status == "approval expired"  # a person was asked; nobody answered in 7 days


async def test_overnight_budget_allows_many_sessions():
    state, approvals = await run_workflow(["rejected"] * 5 + ["released"], mode="auto_if_clean", max_sessions=8)
    assert len(state.sessions) == 6 and approvals


# --- the OpenAPI description must match the code it describes -----------------------

def documented_operations():
    return {(path, method.lower()): op
            for path, methods in server.OPENAPI["paths"].items()
            for method, op in methods.items()}


def http_routes():
    """Every /api route the launcher actually serves, as (path, method)."""
    routes = {}
    for route in server.app.routes:
        path, methods = getattr(route, "path", ""), getattr(route, "methods", set()) or set()
        if path.startswith("/api/"):
            for method in methods - {"HEAD", "OPTIONS"}:
                routes[(path, method.lower())] = route
    return routes


def test_every_http_route_is_documented():
    assert set(http_routes()) == set(documented_operations())


async def test_operation_ids_match_the_mcp_tool_names():
    tools = {t.name for t in await server.mcp.list_tools()}
    assert {op["operationId"] for op in documented_operations().values()} == tools
    assert tools == {"list_problems", "start_research", "research_status", "decide"}


def test_documented_bodies_match_the_implementations():
    """A documented request body names exactly the implementation's own arguments."""
    import inspect
    implementations = {"start_research": server.do_start_research, "decide": server.do_decide}
    for op in documented_operations().values():
        body = op.get("requestBody")
        if not body:
            continue
        schema = body["content"]["application/json"]["schema"]
        taken = set(inspect.signature(implementations[op["operationId"]]).parameters) - {"front_door", "user"}
        documented = set(schema["properties"])
        path_params = {p["name"] for p in op.get("parameters", [])}
        assert documented | path_params == taken, op["operationId"]
        assert set(schema["required"]) == documented


def test_every_operation_is_described_for_a_reader():
    for (path, method), op in documented_operations().items():
        assert op.get("summary"), f"{method} {path} has no summary"
        assert op.get("description"), f"{method} {path} has no description"
        assert "200" in op["responses"]
