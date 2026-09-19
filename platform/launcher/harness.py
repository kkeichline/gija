"""One harness session: a Claude Code pod that runs a task headlessly, then exits.

The pod is the same sandbox OpenClaw's ACP path uses (namespace `agents`, label
`app=acp-harness`, so the same egress policy applies: the model and the governed
gateway, nothing else). Instead of ACP it runs `claude -p` and prints one JSON result,
which is simpler to supervise from a workflow. The pod name is also the gateway
session id (the image's .mcp.json sends it as X-Session-Id), so the gateway's audit
log and run budget line up with the session.
"""

import json
import secrets

from kubernetes import client

NAMESPACE = "agents"
IMAGE = "harness-claude:0.1"
CLAUDE = ("/usr/local/lib/node_modules/@agentclientprotocol/claude-agent-acp/node_modules/"
          "@anthropic-ai/claude-agent-sdk-linux-arm64/claude")
TERMINAL = {"Succeeded", "Failed"}


def start(task: str, max_turns: int = 20, deadline_seconds: int = 1800, *, role: str = "researcher",
          system_prompt: str = "", json_schema: dict | None = None, name: str = "") -> str:
    """Start a harness pod. A `researcher` gets the governed MCP tools; a `reviewer` gets no tools,
    no gateway token, and a network policy that reaches only the model."""
    name = name or f"{'research' if role == 'researcher' else 'review'}-{secrets.token_hex(4)}"
    if role == "researcher":
        labels = {"app": "acp-harness", "harness": "claude", "started-by": "launcher"}
        args = ["--allowedTools", "mcp__governed"]
        workdir = "/workspace"
        env_from = [client.V1EnvFromSource(secret_ref=client.V1SecretEnvSource(name="harness-claude")),
                    client.V1EnvFromSource(config_map_ref=client.V1ConfigMapEnvSource(name="harness-model",
                                                                                     optional=True))]
        env = []
    else:
        labels = {"app": "reviewer", "harness": "claude", "started-by": "launcher"}
        args = ["--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}']
        if system_prompt:
            args += ["--system-prompt", system_prompt]
        if json_schema:
            args += ["--json-schema", json.dumps(json_schema)]
        workdir = "/tmp"  # not /workspace: the researcher's CLAUDE.md and .mcp.json stay out of it
        # Only the model credential, not the gateway token. A reviewer-model ConfigMap, if present,
        # overrides the model so the reviewer can be a different model than the researcher.
        env_from = [client.V1EnvFromSource(config_map_ref=client.V1ConfigMapEnvSource(name="harness-model",
                                                                                     optional=True)),
                    client.V1EnvFromSource(config_map_ref=client.V1ConfigMapEnvSource(name="reviewer-model",
                                                                                     optional=True))]
        env = [client.V1EnvVar(name="ANTHROPIC_API_KEY", value_from=client.V1EnvVarSource(
            secret_key_ref=client.V1SecretKeySelector(name="harness-claude", key="ANTHROPIC_API_KEY")))]
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name=name, labels=labels),
        spec=client.V1PodSpec(
            restart_policy="Never",
            automount_service_account_token=False,
            enable_service_links=False,
            active_deadline_seconds=deadline_seconds,
            security_context=client.V1PodSecurityContext(
                run_as_non_root=True, run_as_user=1000, run_as_group=1000,
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault")),
            containers=[client.V1Container(
                name="claude",
                image=IMAGE,
                image_pull_policy="Never",
                working_dir=workdir,
                command=[CLAUDE, "-p", task, *args, "--max-turns", str(max_turns), "--output-format", "json"],
                env=[client.V1EnvVar(name="POD_NAME", value_from=client.V1EnvVarSource(
                    field_ref=client.V1ObjectFieldSelector(field_path="metadata.name"))), *env],
                env_from=env_from,
                resources=client.V1ResourceRequirements(
                    requests={"cpu": "250m", "memory": "512Mi"}, limits={"cpu": "2", "memory": "2Gi"}),
                security_context=client.V1SecurityContext(
                    allow_privilege_escalation=False, capabilities=client.V1Capabilities(drop=["ALL"])),
            )],
        ),
    )
    client.CoreV1Api().create_namespaced_pod(NAMESPACE, pod)
    return name


def phase(name: str) -> str:
    try:
        return client.CoreV1Api().read_namespaced_pod(name, NAMESPACE).status.phase or "Pending"
    except client.ApiException as exc:
        if exc.status == 404:
            return "Gone"
        raise


def result(name: str) -> dict:
    """Parse Claude Code's final JSON line. The narrative is the agent's; treat it as untrusted."""
    # _preload_content=False: this client version otherwise returns repr(bytes) for logs.
    resp = client.CoreV1Api().read_namespaced_pod_log(name, NAMESPACE, tail_lines=200, _preload_content=False)
    logs = resp.data.decode("utf-8", errors="replace")
    for line in reversed(logs.splitlines()):
        line = line.strip()
        if line.startswith("{") and '"type"' in line:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "result":
                return {"answer": data.get("result", ""), "is_error": bool(data.get("is_error")),
                        "turns": data.get("num_turns"), "duration_s": round((data.get("duration_ms") or 0) / 1000),
                        "structured": data.get("structured_output")}
    return {"answer": "", "is_error": True, "turns": None, "duration_s": None, "structured": None,
            "error": "the harness exited without a result (see the pod log)"}


def delete(name: str) -> None:
    try:
        client.CoreV1Api().delete_namespaced_pod(name, NAMESPACE, grace_period_seconds=0)
    except client.ApiException as exc:
        if exc.status != 404:
            raise

