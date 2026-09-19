"""ResearchRun: the research loop, owned by a workflow instead of by the agent.

    ask -> harness session -> released? --no--> retry with feedback (bounded by sessions + hours)
                                  |yes
                     deterministic checks + reviewer agent
                                  |
          approval.mode: auto            -> promote (recorded as policy)
                         auto_if_clean   -> clean? promote : ask a person (with the flags)
                         human           -> ask a person
                                  |reject / 7 days
                                  stop

The agent decides *how* to answer inside a session; the workflow decides how many
sessions, how long, and whether anything is promoted. The reviewer can only escalate.
The gateway's per-session Cedar budget stays in place as a backstop.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from launcher.activities import (Review, Run, SessionResult, approve_run, fetch_runs, review_run,
                                     run_harness_session)

TASK_QUEUE = "research"
MODES = ("human", "auto_if_clean", "auto")


@dataclass
class ResearchRequest:
    problem: str
    question: str
    requested_by: str
    front_door: str
    max_sessions: int = 4
    max_hours: float = 10
    approval_mode: str = "auto_if_clean"
    reviewer: bool = True


@dataclass
class ResearchState:
    status: str = "starting"
    sessions: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    runs: list[Run] = field(default_factory=list)
    released_run: str = ""
    review: Review | None = None
    escalation: list[str] = field(default_factory=list)  # why a person is being asked
    decided_by: str = ""
    note: str = ""


def task_prompt(req: ResearchRequest, feedback: str) -> str:
    prompt = (f"Work the {req.problem} problem using only the governed MCP tools.\n\n"
              f"Question from {req.requested_by}: {req.question}\n\n"
              "Start with list_problems and read its artifact contract, its how_your_output_is_checked "
              "rules, and its udf_template. Then submit_udf, validate_udf (free; it does not spend your "
              "run budget), fix anything it reports, and only then run_udf. Stop after one released run "
              "and answer the question, citing the run_id.")
    if feedback:
        prompt += f"\n\nA previous attempt did not produce a released result: {feedback}"
    return prompt


def escalation_reasons(req: ResearchRequest, run: Run, review: Review | None) -> list[str]:
    """Why this run is not clean. Empty means auto_if_clean may promote it."""
    reasons = [f"check: {c}" for c in run.checks]
    if req.reviewer:
        if review is None:
            reasons.append("reviewer: no review")
        else:
            if review.risk != "low":
                reasons.append(f"reviewer: risk {review.risk}")
            if not review.answers_the_question:
                reasons.append("reviewer: does not answer the question")
            reasons += [f"reviewer: {f}" for f in review.flags]
    return reasons


@workflow.defn
class ResearchRun:
    def __init__(self) -> None:
        self.state = ResearchState()
        self.decision: tuple[str, str] | None = None  # (approve|reject, user)

    @workflow.run
    async def run(self, req: ResearchRequest) -> ResearchState:
        s = self.state
        deadline = workflow.now() + timedelta(hours=req.max_hours)
        feedback, released = "", None
        for attempt in range(1, req.max_sessions + 1):
            if workflow.now() >= deadline:
                s.note = f"stopped after {req.max_hours}h"
                break
            s.status = f"researching (session {attempt} of {req.max_sessions})"
            name = f"research-{workflow.uuid4().hex[:8]}"
            s.sessions.append(name)  # recorded before the pod starts, so progress is visible at once
            session: SessionResult = await workflow.execute_activity(
                run_harness_session, args=[task_prompt(req, feedback), name],
                start_to_close_timeout=timedelta(minutes=35), heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=2))
            s.answers.append(session.answer or session.error)
            runs: list[Run] = await workflow.execute_activity(
                fetch_runs, session.session, start_to_close_timeout=timedelta(minutes=2))
            s.runs.extend(runs)
            released = next((r for r in reversed(runs) if r.status == "released"), None)
            if released:
                s.released_run = released.run_id
                break
            feedback = "; ".join(f"{r.run_id} {r.status}: {' '.join(r.notes)}" for r in runs) or \
                "no runs were attempted"

        if not released:
            s.status = "finished without a released result"
            return s

        if req.reviewer:
            s.status = "reviewing"
            s.review = await workflow.execute_activity(
                review_run, args=[req.question, released],
                start_to_close_timeout=timedelta(minutes=20), heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=2))
        s.escalation = escalation_reasons(req, released, s.review)

        if req.approval_mode == "auto" or (req.approval_mode == "auto_if_clean" and not s.escalation):
            await self.promote(f"policy:{req.approval_mode}")
            return s

        s.status = "awaiting approval"
        try:
            await workflow.wait_condition(lambda: self.decision is not None, timeout=timedelta(days=7))
        except TimeoutError:
            s.status = "approval expired"
            return s
        verdict, user = self.decision
        if verdict == "approve":
            await self.promote(user)
        else:
            s.decided_by, s.status = user, "rejected"
        return s

    async def promote(self, by: str) -> None:
        await workflow.execute_activity(approve_run, args=[self.state.released_run, by],
                                        start_to_close_timeout=timedelta(minutes=2))
        self.state.decided_by = by
        self.state.status = "approved: UDF promoted" + (" automatically" if by.startswith("policy:") else "")

    @workflow.signal
    def decide(self, verdict: str, user: str) -> None:
        if self.decision is None and self.state.status == "awaiting approval":
            self.decision = (verdict, user)

    @workflow.query
    def status(self) -> ResearchState:
        return self.state
