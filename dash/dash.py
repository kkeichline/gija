"""gija dashboard: the governance view of what the agents are doing.

Everything is read through kubectl, the same access `make audit` uses: the gateway's
persistent audit log and run ledger, harness and job pods, the notifier's wakes, and
OpenClaw's task list. Nothing is deployed to the cluster for this page.

Run: make dash
"""

import datetime as dt
import io
import json
import re
import subprocess

import pandas as pd
import streamlit as st

CONTEXT = "kind-openclaw"
DECISION = {"ALLOW": "✅ allow", "DENY": "⛔ deny"}
RUN_STATUS = {"released": "🟢 released", "rejected": "🟠 rejected", "failed": "🔴 failed",
              "running": "⏳ running", "finalizing": "⏳ finalizing"}

st.set_page_config(page_title="gija", page_icon="🧵", layout="wide")


def kubectl(*args: str, timeout: int = 20) -> str:
    result = subprocess.run(["kubectl", "--context", CONTEXT, *args],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:300])
    return result.stdout


def in_gateway(code: str) -> str:
    return kubectl("exec", "-n", "gateway", "deploy/mcp-gateway", "--", "python", "-c", code)


def local_time(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


# --- data ---------------------------------------------------------------------------

@st.cache_data(ttl=4)
def audit() -> pd.DataFrame:
    lines = kubectl("exec", "-n", "gateway", "deploy/mcp-gateway", "--",
                    "tail", "-n", "3000", "/runs/audit.jsonl").splitlines()
    events = [json.loads(line) for line in lines if line.startswith("{")]
    return pd.DataFrame(events)


@st.cache_data(ttl=4)
def runs() -> pd.DataFrame:
    rows = json.loads(in_gateway(
        "import json,sqlite3;c=sqlite3.connect('/runs/state.db');c.row_factory=sqlite3.Row;"
        "print(json.dumps([dict(r) for r in c.execute('SELECT * FROM runs ORDER BY created DESC')]))"))
    return pd.DataFrame(rows)


@st.cache_data(ttl=30)
def artifact(run_id: str, problem: str) -> str:
    return in_gateway(
        "import boto3;print(boto3.client('s3').get_object(Bucket='artifacts-released',"
        f"Key='{run_id}/result.csv')['Body'].read().decode(),end='')")


@st.cache_data(ttl=4)
def pods() -> pd.DataFrame:
    items = json.loads(kubectl("get", "pods", "-A", "-l", "app in (acp-harness,udf-job)", "-o", "json"))["items"]
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    for p in items:
        started = dt.datetime.fromisoformat(p["metadata"]["creationTimestamp"].replace("Z", "+00:00"))
        rows.append({
            "pod": p["metadata"]["name"],
            "role": "🤖 Claude Code (harness)" if p["metadata"]["labels"].get("app") == "acp-harness" else "⚙️ UDF job",
            "namespace": p["metadata"]["namespace"],
            "phase": p["status"].get("phase", "?"),
            "age": f"{int((now - started).total_seconds() // 60)} min",
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=4)
def wakes() -> list[str]:
    log = kubectl("logs", "-n", "openclaw", "deploy/openclaw", "-c", "notifier", "--since=24h")
    return [line for line in log.splitlines() if "woke OpenClaw" in line or "notify failed" in line]


@st.cache_data(ttl=10)
def acp_tasks() -> pd.DataFrame:
    raw = kubectl("exec", "-n", "openclaw", "deploy/openclaw", "-c", "gateway", "--",
                  "openclaw", "tasks", "list", "--json", "--runtime", "acp", timeout=40)
    start = min(i for i in (raw.find("{"), raw.find("[")) if i >= 0)  # skip any CLI banner
    data = json.loads(raw[start:])
    tasks = data.get("tasks", data) if isinstance(data, dict) else data
    keep = ("createdAt", "status", "summary", "title", "delivery", "childSessionKey")
    df = pd.DataFrame([{k: t.get(k) for k in keep if k in t} for t in tasks])
    if "createdAt" in df:
        df["createdAt"] = df["createdAt"].map(lambda ms: local_time(ms / 1000) if isinstance(ms, (int, float)) else ms)
        df = df.rename(columns={"createdAt": "started"})
    return df


@st.cache_data(ttl=6)
def research_runs() -> pd.DataFrame:
    """Temporal workflows: one row per research run, with its stage and whether it's moving."""
    raw = kubectl("exec", "-n", "temporal", "deploy/temporal", "--", "temporal", "--address",
                  "127.0.0.1:7233", "workflow", "list", "--limit", "20", "--output", "json", timeout=30)
    items = json.loads(raw[raw.find("["):] if raw.lstrip().startswith("[") else raw[raw.find("{"):])
    items = items.get("executions", items) if isinstance(items, dict) else items
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    for w in items:
        info = w.get("execution", w)
        started = dt.datetime.fromisoformat(str(w.get("startTime", "")).replace("Z", "+00:00"))
        status = str(w.get("status", "")).replace("WORKFLOW_EXECUTION_STATUS_", "").title()
        rows.append({"research": info.get("workflowId", ""),
                     "state": {"Running": "⏳ running", "Completed": "🟢 done"}.get(status, f"🔴 {status}"),
                     "started": started.astimezone().strftime("%H:%M:%S"),
                     "elapsed": f"{int((now - started).total_seconds() // 60)} min"})
    return pd.DataFrame(rows)


@st.cache_data(ttl=10)
def active_model() -> str:
    return kubectl("get", "configmap", "openclaw-model", "-n", "openclaw",
                   "-o", "jsonpath={.data.OPENCLAW_MODEL}") or "unknown"


def count_warning(csv_text: str) -> str | None:
    """The release rule trusts n_members from the UDF. Flag the obvious fabrication pattern."""
    df = pd.read_csv(io.StringIO(csv_text))
    if "n_members" in df and len(df) >= 5 and df["n_members"].nunique() == 1:
        return (f"Every row claims n_members = {df['n_members'].iloc[0]}. Group sizes were probably "
                "written by the UDF, not counted, so small-group suppression may not have applied.")
    return None


# --- page ----------------------------------------------------------------------------

st.title("🧵 gija")
st.caption("The governance view: every tool call the agents make, what the gateway decided, "
           "and what was released. Coordination and chat live in the OpenClaw Control UI.")


@st.fragment(run_every="5s")
def live() -> None:
    try:
        events, ledger, live_pods = audit(), runs(), pods()
    except Exception as exc:  # cluster down, gateway restarting, ...
        st.error(f"Can't reach the cluster: {exc}")
        return

    top = st.columns([3, 1, 1])
    top[0].markdown(f"**Model:** `{active_model()}` · refreshed {dt.datetime.now():%H:%M:%S}")
    top[1].link_button("OpenClaw UI ↗", "http://127.0.0.1:18789", help="Needs `make ui` running")
    top[2].link_button("Hubble ↗", "http://127.0.0.1:12000", help="Needs `make hubble` running")

    authz = events[events["event"] == "authz"] if not events.empty else events
    status = ledger["status"].value_counts() if not ledger.empty else pd.Series(dtype=int)
    tiles = st.columns(6)
    tiles[0].metric("Tool calls allowed", int((authz["decision"] == "ALLOW").sum()) if not authz.empty else 0)
    tiles[1].metric("Tool calls denied", int((authz["decision"] == "DENY").sum()) if not authz.empty else 0)
    tiles[2].metric("Runs released", int(status.get("released", 0)))
    tiles[3].metric("Runs rejected", int(status.get("rejected", 0)))
    tiles[4].metric("Runs failed", int(status.get("failed", 0)))
    tiles[5].metric("Pods live now", int((live_pods["phase"] == "Running").sum()) if not live_pods.empty else 0)

    sessions = (events.dropna(subset=["session"]).sort_values("ts", ascending=False)["session"]
                .drop_duplicates().tolist() if "session" in events else [])
    session = st.selectbox("Agent session", ["All sessions", *sessions], key="session",
                           help="Each harness pod is its own session; its run budget is counted per session.")

    view = events if session == "All sessions" else events[events.get("session") == session]
    left, right = st.columns([3, 2])

    with left:
        st.subheader("Decision timeline")
        if view.empty:
            st.info("No gateway activity yet.")
        else:
            t = view.sort_values("ts", ascending=False).head(200)
            table = pd.DataFrame({
                "time": t["ts"].map(local_time),
                "session": t.get("session"),
                "event": t["event"].str.replace("_", " "),
                "tool": t.get("action", pd.Series(dtype=str)).fillna(""),
                "resource / run": t.get("resource", pd.Series(dtype=str))
                                  .fillna(t.get("run_id", pd.Series(dtype=str)))
                                  .fillna(t.get("key", pd.Series(dtype=str))).fillna(""),
                "outcome": t.get("decision", pd.Series(dtype=str)).map(DECISION)
                           .fillna(t.get("status", pd.Series(dtype=str)).map(RUN_STATUS)).fillna(""),
                "policy": t.get("policies", pd.Series(dtype=object)).map(
                    lambda p: ", ".join(p) if isinstance(p, list) else ""),
                "notes": t.get("notes", pd.Series(dtype=object)).map(
                    lambda n: "; ".join(n) if isinstance(n, list) else "").fillna(""),
            })
            st.dataframe(table, hide_index=True, use_container_width=True, height=420)

    with right:
        st.subheader("Runs")
        runs_view = ledger if session == "All sessions" or ledger.empty else ledger[ledger["session"] == session]
        if runs_view.empty:
            st.info("No runs yet.")
        else:
            st.dataframe(pd.DataFrame({
                "run": runs_view["run_id"],
                "started": runs_view["created"].map(local_time),
                "outcome": runs_view["status"].map(RUN_STATUS).fillna(runs_view["status"]),
                "rows out": runs_view["rows"],
                "notes": runs_view["notes"].map(lambda n: "; ".join(json.loads(n or "[]"))),
                "checks": (runs_view["checks"] if "checks" in runs_view else pd.Series([""] * len(runs_view)))
                          .map(lambda c: "🚩 " + "; ".join(json.loads(c)) if c and c != "[]" else ""),
            }), hide_index=True, use_container_width=True, height=240)

            released = runs_view[runs_view["status"] == "released"]["run_id"].tolist()
            if released:
                pick = st.selectbox("Released artifact", released, key="artifact")
                csv_text = artifact(pick, "")
                warning = count_warning(csv_text)
                if warning:
                    st.warning(warning, icon="⚠️")
                st.dataframe(pd.read_csv(io.StringIO(csv_text)), hide_index=True,
                             use_container_width=True, height=200)

    bottom = st.columns([2, 2, 3])
    with bottom[0]:
        st.subheader("Pods")
        if live_pods.empty:
            st.caption("No harness or job pods right now.")
        else:
            st.dataframe(live_pods, hide_index=True, use_container_width=True)
    with bottom[1]:
        st.subheader("Notifications to OpenClaw")
        notes = wakes()
        st.caption("SNS → SQS → notifier → OpenClaw /hooks/wake (the EventBridge + SNS stand-in)")
        for line in reversed(notes[-8:]):
            m = re.search(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*?(woke OpenClaw for \S+|notify failed.*)", line)
            if not m:
                st.text(line)
                continue
            utc = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
            st.text(f"{utc.astimezone():%H:%M:%S}  {m.group(2)}")  # the pod logs in UTC
    with bottom[2]:
        st.subheader("Research runs")
        st.caption("Temporal workflows: ask → sessions → review → approval. `make temporal` for full history.")
        try:
            research = research_runs()
            st.dataframe(research, hide_index=True, use_container_width=True, height=180) if not research.empty \
                else st.caption("No research runs yet.")
        except Exception as exc:
            st.caption(f"Couldn't read Temporal ({str(exc)[:120]}).")
        st.subheader("OpenClaw ACP tasks")
        try:
            tasks = acp_tasks()
            if tasks.empty:
                st.caption("No delegated ACP sessions yet.")
            else:
                st.dataframe(tasks, hide_index=True, use_container_width=True)
        except Exception as exc:
            st.caption(f"Couldn't read OpenClaw's task list ({exc}).")


live()
