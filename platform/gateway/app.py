"""Governed MCP gateway: every tool call is authenticated, checked by Cedar, and audited.

Local stand-in for AgentCore Gateway + Cedar in front of SageMaker Processing:

    submit_udf -> UDF staged in the S3 staging bucket (moto)
    run_udf    -> filtered snapshot -> network-isolated Kubernetes Job -> release rule
    get_run    -> status plus the released artifact, never the raw output

The agent holds only a bearer token for this service. The storage, cluster, and
notification credentials live here, so there is no path to the data that skips
these checks.
"""

import ast
import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

import boto3
import cedarpy
from botocore.exceptions import EndpointConnectionError
import yaml
from kubernetes import client, config as k8s_config
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from gateway import checks, lake, release

PLATFORM = Path(os.environ.get("PLATFORM_DIR", "/platform"))
RUNS = Path(os.environ.get("RUNS_DIR", "/runs"))
LAKE = Path(os.environ.get("LAKE_DIR", "/lake"))
JOBS_NS = os.environ.get("JOBS_NAMESPACE", "jobs")
RUNNER_IMAGE = os.environ.get("RUNNER_IMAGE", "udf-runner:0.1")
WAIT_SECONDS = int(os.environ.get("RUN_WAIT_SECONDS", "90"))
STAGING_BUCKET = "udf-staging"
RELEASED_BUCKET = "artifacts-released"
ALLOWED_IMPORTS = {"pandas", "numpy", "math", "statistics", "re", "datetime", "collections", "itertools"}
ID = re.compile(r"^[a-z0-9_-]{1,64}$")

CATALOG = yaml.safe_load((PLATFORM / "catalog.yaml").read_text())
CONTRACTS = {
    c["problem"]: c
    for c in (yaml.safe_load(p.read_text()) for p in sorted((PLATFORM / "contracts").glob("*.yaml")))
}
POLICIES = (PLATFORM / "policies" / "gateway.cedar").read_text()
POLICY_IDS = re.findall(r'@id\("([^"]+)"\)', POLICIES)  # cedar names them policy0, policy1, ... in file order
TOKENS: dict[str, str] = json.loads(os.environ.get("AGENT_TOKENS", "{}"))  # token -> agent id, from a Secret
PLATFORM_TOKEN = os.environ.get("PLATFORM_TOKEN", "")  # for the launcher's /internal calls, never given to agents

log = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def audit(**event) -> None:
    event["ts"] = round(time.time(), 3)
    line = json.dumps(event, default=str)
    log.info(line)
    with open(RUNS / "audit.jsonl", "a") as f:
        f.write(line + "\n")


# --- state -------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    con = sqlite3.connect(RUNS / "state.db", isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def init_state() -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    db().execute("""CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, agent TEXT, session TEXT, problem TEXT, dataset TEXT,
        udf_id TEXT, status TEXT, rows INTEGER, notes TEXT, created REAL, finished REAL)""")
    cols = {r[1] for r in db().execute("PRAGMA table_info(runs)")}
    if "checks" not in cols:  # added with the deterministic checks; older ledgers get the column
        db().execute("ALTER TABLE runs ADD COLUMN checks TEXT DEFAULT '[]'")


def runs_used(agent: str, session: str, problem: str) -> int:
    return db().execute(
        "SELECT count(*) FROM runs WHERE agent=? AND session=? AND problem=?", (agent, session, problem)
    ).fetchone()[0]


# --- AWS stand-ins (moto) ------------------------------------------------------------
# boto3 honours AWS_ENDPOINT_URL, so pointing this at real AWS is a config change.

s3 = boto3.client("s3")
sns = boto3.client("sns")
TOPIC_ARN = ""


def init_aws() -> None:
    global TOPIC_ARN
    for _ in range(30):  # moto may still be starting
        try:
            for bucket in (STAGING_BUCKET, RELEASED_BUCKET):
                try:
                    s3.create_bucket(Bucket=bucket)
                except s3.exceptions.BucketAlreadyOwnedByYou:
                    pass
            TOPIC_ARN = sns.create_topic(Name="run-events")["TopicArn"]
            return
        except EndpointConnectionError:
            time.sleep(2)
    raise RuntimeError("moto is unreachable")


# --- authn / authz -------------------------------------------------------------------

ENTITIES = (
    [{"uid": {"type": "Agent", "id": a}, "attrs": {"team": v["team"], "clearances": v["clearances"]}, "parents": []}
     for a, v in CATALOG["agents"].items()]
    + [{"uid": {"type": "Dataset", "id": d}, "attrs": {"sensitivity": v["sensitivity"], "problems": v["problems"]},
        "parents": []} for d, v in CATALOG["datasets"].items()]
    + [{"uid": {"type": "Problem", "id": p}, "attrs": {"teams": c["teams"]}, "parents": []}
       for p, c in CONTRACTS.items()]
)


def caller(ctx: Context) -> tuple[str, str]:
    """Map the bearer token to an agent id. The session header only scopes budgets; it grants nothing."""
    headers = ctx.headers or {}
    agent = TOKENS.get(headers.get("authorization", "").removeprefix("Bearer ").strip())
    if agent is None:
        audit(event="authn", decision="DENY", reason="missing or unknown bearer token")
        raise ToolError("unauthenticated")
    session = re.sub(r"[^a-z0-9-]", "", headers.get("x-session-id", "").lower())[:63] or "default"
    return agent, session


def authorize(agent, session, action, resource_type, resource_id, context=None, extra_entities=()):
    if not ID.match(resource_id):
        audit(event="authz", agent=agent, session=session, action=action, decision="DENY", reason="malformed id")
        raise ToolError(f"malformed {resource_type.lower()} id")
    request = {
        "principal": f'Agent::"{agent}"',
        "action": f'Action::"{action}"',
        "resource": f'{resource_type}::"{resource_id}"',
        "context": context or {},
    }
    result = cedarpy.is_authorized(request, POLICIES, ENTITIES + list(extra_entities))
    fired = [POLICY_IDS[int(r[6:])] if r.startswith("policy") and r[6:].isdigit() else r
             for r in result.diagnostics.reasons]
    audit(event="authz", agent=agent, session=session, action=action,
          resource=f"{resource_type}::{resource_id}", context=context or {},
          decision="ALLOW" if result.allowed else "DENY", policies=fired,
          errors=[str(e) for e in result.diagnostics.errors])
    if not result.allowed:
        why = f"forbidden by {', '.join(fired)}" if fired else "no policy permits it"
        raise ToolError(f"DENIED: {action} on {resource_type}::{resource_id} ({why})")


# --- the job (SageMaker Processing stand-in) ---------------------------------------------

def launch_job(run_id: str, contract: dict) -> None:
    job = contract["job"]

    def mount(sub, path, read_only):
        return client.V1VolumeMount(name="runs", mount_path=path, sub_path=f"{run_id}/{sub}", read_only=read_only)

    pod = client.V1PodSpec(
        restart_policy="Never",
        automount_service_account_token=False,
        enable_service_links=False,
        security_context=client.V1PodSecurityContext(
            run_as_non_root=True, run_as_user=10001, run_as_group=10001,
            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault")),
        containers=[client.V1Container(
            name="udf",
            image=RUNNER_IMAGE,
            image_pull_policy="Never",
            env=[client.V1EnvVar(name="ARTIFACT_FILE", value=contract["artifact"]["file"])],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "100m", "memory": "256Mi"},
                limits={"cpu": job["cpu"], "memory": job["memory"]}),
            security_context=client.V1SecurityContext(
                allow_privilege_escalation=False, read_only_root_filesystem=True,
                capabilities=client.V1Capabilities(drop=["ALL"])),
            volume_mounts=[
                mount("input", "/opt/ml/processing/input", True),
                mount("code", "/opt/ml/processing/code", True),
                mount("output", "/opt/ml/processing/output", False),
                client.V1VolumeMount(name="tmp", mount_path="/tmp"),
            ])],
        volumes=[
            client.V1Volume(name="runs", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name="runs")),
            client.V1Volume(name="tmp", empty_dir=client.V1EmptyDirVolumeSource(size_limit="256Mi")),
        ])
    labels = {"app": "udf-job", "run-id": run_id}
    client.BatchV1Api().create_namespaced_job(JOBS_NS, client.V1Job(
        metadata=client.V1ObjectMeta(name=run_id, labels=labels),
        spec=client.V1JobSpec(
            backoff_limit=0,
            active_deadline_seconds=job["timeout_seconds"],
            ttl_seconds_after_finished=3600,
            template=client.V1PodTemplateSpec(metadata=client.V1ObjectMeta(labels=labels), spec=pod))))


def job_outcome(run_id: str) -> tuple[str, str] | None:
    """Return (succeeded|failed, detail) once the Job is done, else None."""
    status = client.BatchV1Api().read_namespaced_job_status(run_id, JOBS_NS).status
    if status.succeeded:
        return "succeeded", ""
    if not status.failed and not any(c.type == "Failed" and c.status == "True" for c in status.conditions or []):
        return None
    detail = next((c.reason for c in status.conditions or [] if c.type == "Failed"), "failed")
    pods = client.CoreV1Api().list_namespaced_pod(JOBS_NS, label_selector=f"run-id={run_id}").items
    for p in pods:
        for cs in p.status.container_statuses or []:
            if cs.state.terminated and cs.state.terminated.message:
                # Untrusted text from the UDF, truncated. A known (narrow) leak channel.
                detail = cs.state.terminated.message[:300]
    return "failed", detail


def finalize(run_id: str) -> None:
    """Apply the release rule once, publish the outcome, and record it. Safe to call repeatedly."""
    con = db()
    if con.execute("UPDATE runs SET status='finalizing' WHERE run_id=? AND status='running'", (run_id,)).rowcount != 1:
        return
    row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    contract = CONTRACTS[row["problem"]]
    outcome, detail = job_outcome(run_id)
    rows, notes, flags = 0, [], []
    if outcome == "failed":
        status, notes = "failed", [detail]
    else:
        rel = release.apply(contract, RUNS / run_id / "output", RUNS / run_id / "input" / "data.parquet")
        status, rows, notes = ("released" if rel.released else "rejected"), rel.rows, rel.notes
        if rel.released:
            flags = checks.run(contract, (RUNS / run_id / "code" / "udf.py").read_text(), rel.csv_text)
            s3.put_object(Bucket=RELEASED_BUCKET, Key=f"{run_id}/{contract['artifact']['file']}", Body=rel.csv_text)
            s3.put_object_tagging(Bucket=STAGING_BUCKET, Key=f"{row['problem']}/{row['udf_id']}.py",
                                  Tagging={"TagSet": [{"Key": "released", "Value": "true"}]})
    con.execute("UPDATE runs SET status=?, rows=?, notes=?, finished=?, checks=? WHERE run_id=?",
                (status, rows, json.dumps(notes), time.time(), json.dumps(flags), run_id))
    event = {"run_id": run_id, "agent": row["agent"], "session": row["session"], "problem": row["problem"],
             "dataset": row["dataset"], "status": status, "rows": rows, "notes": notes, "checks": flags}
    sns.publish(TopicArn=TOPIC_ARN, Subject=f"run {status}", Message=json.dumps(event))
    audit(event="run_finished", **event)


async def watch(run_id: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(job_outcome, run_id):
            await asyncio.to_thread(finalize, run_id)
            return True
        await asyncio.sleep(2)
    return False


def view(run_id: str) -> dict:
    row = db().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    out = {k: row[k] for k in ("run_id", "problem", "dataset", "udf_id", "status", "rows")}
    out["notes"] = json.loads(row["notes"] or "[]")
    if row["status"] == "released":
        key = f"{run_id}/{CONTRACTS[row['problem']]['artifact']['file']}"
        out["artifact_csv"] = s3.get_object(Bucket=RELEASED_BUCKET, Key=key)["Body"].read().decode()
    return out


# --- MCP tools -------------------------------------------------------------------------

mcp = MCPServer(
    name="governed-gateway",
    instructions=(
        "The only way to reach data. Call list_problems first: it gives you the artifact contract, "
        "a UDF template, and exactly how your output will be checked. Submit a UDF defining "
        "run(df: pandas.DataFrame) -> pandas.DataFrame, call validate_udf (free, no budget), fix "
        "anything it reports, then run_udf. Only output matching the contract is returned."
    ),
)


def how_output_is_checked(contract: dict) -> list[str]:
    """The rules the release rule applies, in the agent's own terms. No surprises."""
    a = contract["artifact"]
    rules = [f"Write exactly these columns, in this order: {[c['name'] for c in a['columns']]}.",
             f"At most {a['max_rows']} rows and {a['max_bytes']} bytes."]
    if a.get("group_by"):
        rules.append(f"One row per group of {a['group_by']}. The platform recomputes each group from the "
                     "snapshot: groups that don't exist are rejected.")
    if a.get("min_group_size"):
        k = a["min_group_size"]
        rules.append(f"{k['column']} must equal the group's true size, which the platform counts itself. "
                     f"Writing a number that doesn't match gets the whole artifact rejected; groups smaller "
                     f"than {k['k']} are suppressed for privacy (that is normal, not an error).")
    for name, v in (a.get("verify") or {}).items():
        rules.append(f"{name} must equal {v['aggregate']}({v['of']}) for the group; the platform recomputes it.")
    rules.append("Released results are reviewed for hard-coded values, for gaming these rules, and for "
                 "whether they actually answer the question. Compute everything from the data you are given.")
    return rules


def udf_template(contract: dict) -> str:
    a = contract["artifact"]
    keys = a.get("group_by") or []
    counter = (a.get("min_group_size") or {}).get("column")
    aggs = [f'{counter}=("{keys[0] if keys else "x"}", "size")'] if counter else []
    aggs += [f'{name}=("{v["of"]}", "{v["aggregate"]}")' for name, v in (a.get("verify") or {}).items()]
    return (f"""import pandas as pd

def run(df):
    keys = {keys}
    out = df.groupby(keys).agg({", ".join(aggs)}).reset_index()
    return out[{[c["name"] for c in a["columns"]]}]
""")


@mcp.tool()
async def list_problems(ctx: Context) -> dict:
    """List the problems you may work on, with their artifact contracts, datasets, and remaining run budget."""
    agent, session = caller(ctx)
    authorize(agent, session, "list_problems", "Catalog", "all")
    team = CATALOG["agents"][agent]["team"]
    problems = []
    for pid, c in CONTRACTS.items():
        if team not in c["teams"]:
            continue
        datasets = {
            d: {"sensitivity": CATALOG["datasets"][d]["sensitivity"],
                "columns": CATALOG["datasets"][d]["grants"].get(team, {}).get("columns", [])}
            for d in CATALOG["datasets"] if pid in CATALOG["datasets"][d]["problems"]
        }
        problems.append({
            "problem": pid,
            "question": c["question"].strip(),
            "datasets": datasets,
            "artifact_contract": c["artifact"],
            "how_your_output_is_checked": how_output_is_checked(c),
            "udf_template": udf_template(c),
            "udf_interface": "def run(df: pandas.DataFrame) -> pandas.DataFrame  # df has only the columns listed",
            "allowed_imports": sorted(ALLOWED_IMPORTS),
            "runs_remaining": c["max_runs"] - runs_used(agent, session, pid),
            "advice": ("Call validate_udf first: it checks your UDF against the contract on a small sample "
                       "and does NOT use your run budget. Only then call run_udf."),
        })
    return {"agent": agent, "session": session, "problems": problems}


@mcp.tool()
async def submit_udf(problem: str, code: str, ctx: Context) -> dict:
    """Stage a Python UDF for a problem. It must define run(df) -> DataFrame. Returns a udf_id."""
    agent, session = caller(ctx)
    authorize(agent, session, "submit_udf", "Problem", problem, {"code_bytes": len(code.encode())})
    if problem not in CONTRACTS:
        raise ToolError(f"unknown problem {problem}")
    # A lint for fast feedback, not the security boundary; the sandboxed job is.
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ToolError(f"UDF does not parse: {exc}")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in tree.body):
        raise ToolError("UDF must define a top-level function run(df)")
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    if imported - ALLOWED_IMPORTS:
        raise ToolError(f"UDF imports outside the allowlist: {sorted(imported - ALLOWED_IMPORTS)}")

    udf_id = "udf-" + hashlib.sha256(code.encode()).hexdigest()[:12]
    key = f"{problem}/{udf_id}.py"
    s3.put_object(Bucket=STAGING_BUCKET, Key=key, Body=code.encode(),
                  Metadata={"agent": agent, "session": session}, Tagging="released=false")
    audit(event="udf_staged", agent=agent, session=session, key=f"s3://{STAGING_BUCKET}/{key}")
    return {"udf_id": udf_id, "staged_at": f"s3://{STAGING_BUCKET}/{key}"}


@mcp.tool()
async def run_udf(problem: str, udf_id: str, dataset: str, ctx: Context) -> dict:
    """Run a staged UDF against a dataset in a network-isolated job. Counts against the run budget."""
    agent, session = caller(ctx)
    contract = CONTRACTS.get(problem)
    if contract is None or dataset not in CATALOG["datasets"] or not ID.match(udf_id):
        raise ToolError("unknown problem, dataset, or udf_id")
    used = runs_used(agent, session, problem)
    authorize(agent, session, "run_udf", "Dataset", dataset, {
        "problem": problem, "problem_teams": contract["teams"],
        "runs_used": used, "max_runs": contract["max_runs"],
    })
    try:
        code = s3.get_object(Bucket=STAGING_BUCKET, Key=f"{problem}/{udf_id}.py")["Body"].read()
    except s3.exceptions.NoSuchKey:
        raise ToolError(f"no staged UDF {udf_id} for {problem}")

    run_id = "run-" + uuid.uuid4().hex[:10]
    run_dir = RUNS / run_id
    for sub in ("input", "code", "output"):
        (run_dir / sub).mkdir(parents=True)
    team = CATALOG["agents"][agent]["team"]
    snapshot_rows = await asyncio.to_thread(lake.snapshot, CATALOG, dataset, team, LAKE, run_dir / "input" / "data.parquet")
    (run_dir / "code" / "udf.py").write_bytes(code)
    db().execute("INSERT INTO runs (run_id, agent, session, problem, dataset, udf_id, status, rows, notes, created)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (run_id, agent, session, problem, dataset, udf_id, "running", 0, "[]", time.time()))
    await asyncio.to_thread(launch_job, run_id, contract)
    audit(event="run_started", agent=agent, session=session, run_id=run_id, dataset=dataset,
          snapshot_rows=snapshot_rows, runs_used=used + 1, max_runs=contract["max_runs"])

    if await watch(run_id, WAIT_SECONDS):
        return view(run_id)
    asyncio.create_task(watch(run_id, contract["job"]["timeout_seconds"] + 60))
    return {"run_id": run_id, "status": "running", "hint": "call get_run(run_id) shortly"}


@mcp.tool()
async def validate_udf(problem: str, udf_id: str, dataset: str, ctx: Context) -> dict:
    """Dry-run a staged UDF on a small sample and report contract problems. Free: no run budget used."""
    agent, session = caller(ctx)
    contract = CONTRACTS.get(problem)
    if contract is None or dataset not in CATALOG["datasets"] or not ID.match(udf_id):
        raise ToolError("unknown problem, dataset, or udf_id")
    authorize(agent, session, "run_udf", "Dataset", dataset, {
        "problem": problem, "problem_teams": contract["teams"],
        "runs_used": 0, "max_runs": contract["max_runs"],  # a dry run never spends the budget
    })
    try:
        code = s3.get_object(Bucket=STAGING_BUCKET, Key=f"{problem}/{udf_id}.py")["Body"].read()
    except s3.exceptions.NoSuchKey:
        raise ToolError(f"no staged UDF {udf_id} for {problem}")

    run_id = "dry-" + uuid.uuid4().hex[:10]
    run_dir = RUNS / run_id
    for sub in ("input", "code", "output"):
        (run_dir / sub).mkdir(parents=True)
    team = CATALOG["agents"][agent]["team"]
    rows = await asyncio.to_thread(lake.snapshot, CATALOG, dataset, team, LAKE,
                                   run_dir / "input" / "data.parquet", 2000)
    (run_dir / "code" / "udf.py").write_bytes(code)
    await asyncio.to_thread(launch_job, run_id, contract)
    audit(event="udf_validated", agent=agent, session=session, dry_run=run_id, udf_id=udf_id, sample_rows=rows)

    outcome = None
    for _ in range(60):
        outcome = await asyncio.to_thread(job_outcome, run_id)
        if outcome:
            break
        await asyncio.sleep(2)
    if outcome is None:
        return {"ok": False, "problems": ["the validation job did not finish in time"]}
    if outcome[0] == "failed":
        return {"ok": False, "problems": [f"your UDF raised: {outcome[1]}"], "sample_rows": rows}

    # Same release rule, minus suppression: on a sample almost every group is small.
    sample_contract = json.loads(json.dumps(contract))
    sample_contract["artifact"].pop("min_group_size", None)
    rel = release.apply(sample_contract, run_dir / "output", run_dir / "input" / "data.parquet")
    flags = checks.run(contract, code.decode("utf-8", "replace"), rel.csv_text) if rel.released else []
    return {"ok": bool(rel.released) and not flags,
            "problems": ([] if rel.released else rel.notes) + flags,
            "sample_rows": rows,
            "note": ("Validation ran on a sample and skipped small-group suppression; the real run applies it. "
                     "Nothing here counts against your run budget.")}


@mcp.tool()
async def get_run(run_id: str, ctx: Context) -> dict:
    """Get a run's status and, if released, its artifact."""
    agent, session = caller(ctx)
    row = db().execute("SELECT agent FROM runs WHERE run_id=?", (run_id,)).fetchone()
    owner = row["agent"] if row else "nobody"
    authorize(agent, session, "get_run", "Run", run_id, extra_entities=[
        {"uid": {"type": "Run", "id": run_id}, "attrs": {"owner": {"__entity": {"type": "Agent", "id": owner}}},
         "parents": []}])
    if row is None:
        raise ToolError("unknown run")
    await watch(run_id, 0.1)
    return view(run_id)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


# --- internal API for the research launcher (not MCP; agents can't reach or use it) ------

def platform_caller(request: Request) -> bool:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    ok = bool(PLATFORM_TOKEN) and token == PLATFORM_TOKEN
    if not ok:
        audit(event="internal_authn", decision="DENY", path=request.url.path)
    return ok


@mcp.custom_route("/internal/sessions/{session}/runs", methods=["GET"])
async def session_runs(request: Request) -> JSONResponse:
    """What a harness session did: each run's outcome and, if released, the released artifact."""
    if not platform_caller(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    session = request.path_params["session"]
    rows = db().execute("SELECT run_id FROM runs WHERE session=? ORDER BY created", (session,)).fetchall()
    for r in rows:
        await watch(r["run_id"], 0.1)  # finalize anything finished but not yet collected
    runs = []
    for r in rows:
        v = view(r["run_id"])
        v["checks"] = json.loads(db().execute("SELECT checks FROM runs WHERE run_id=?", (r["run_id"],))
                                 .fetchone()[0] or "[]")
        code = RUNS / r["run_id"] / "code" / "udf.py"
        v["udf_code"] = code.read_text()[:20000] if code.exists() else ""  # for the reviewer; never sent to agents
        runs.append(v)
    return JSONResponse({"session": session, "runs": runs})


@mcp.custom_route("/internal/sessions/{session}/activity", methods=["GET"])
async def session_activity(request: Request) -> JSONResponse:
    """The session's recent audit events, so a workflow can show progress (and spot a stall)."""
    if not platform_caller(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    session = request.path_params["session"]
    events = []
    with open(RUNS / "audit.jsonl") as f:
        for line in f:
            if f'"session": "{session}"' in line:
                e = json.loads(line)
                events.append({"ts": e["ts"], "event": e["event"], "action": e.get("action", ""),
                               "decision": e.get("decision", ""), "run_id": e.get("run_id", "")})
    return JSONResponse({"session": session, "events": events[-25:], "count": len(events)})


@mcp.custom_route("/internal/runs/{run_id}/approve", methods=["POST"])
async def approve_run(request: Request) -> JSONResponse:
    """Record a human approval on a released run's UDF; the mover promotes only approved UDFs."""
    if not platform_caller(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    run_id = request.path_params["run_id"]
    approver = (await request.json()).get("approved_by", "")
    row = db().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None or row["status"] != "released":
        return JSONResponse({"error": "only released runs can be approved"}, status_code=409)
    key = f"{row['problem']}/{row['udf_id']}.py"
    s3.put_object_tagging(Bucket=STAGING_BUCKET, Key=key, Tagging={"TagSet": [
        {"Key": "released", "Value": "true"}, {"Key": "approved", "Value": "true"},
        {"Key": "approved_by", "Value": re.sub(r"[^\w.@+-]", "_", approver)[:128]}]})
    audit(event="udf_approved", run_id=run_id, udf_id=row["udf_id"], approved_by=approver,
          key=f"s3://{STAGING_BUCKET}/{key}")
    return JSONResponse({"run_id": run_id, "udf_id": row["udf_id"], "approved_by": approver})


init_state()
if os.environ.get("AWS_ENDPOINT_URL"):
    init_aws()
if os.environ.get("KUBERNETES_SERVICE_HOST"):
    k8s_config.load_incluster_config()

# Inside the cluster the Service name is the Host header; NetworkPolicy, not
# DNS-rebinding checks, is what limits who can reach this port.
app = mcp.streamable_http_app(
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
