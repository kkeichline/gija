# gija

gija runs AI research agents against data they are not allowed to read directly.

An agent writes code. The platform runs that code in a sandbox, checks the output
against a declared contract, and returns only what passes. Every call the agent makes is
authorized and logged.

*Gija* is Lithuanian for "thread".

## Status

gija is a prototype. It runs on one machine in a local Kubernetes cluster. The included
dataset is synthetic. Do not run it against production data.

## What it does

A research run has seven steps:

1. A person asks a question through a front door: a chat application or the command line.
2. A workflow starts a coding agent, Claude Code, in a pod with no data access.
3. The agent reads the problem's contract and writes a user-defined function (UDF) in
   Python.
4. The platform copies a filtered view of the dataset. It runs the UDF on that copy in a
   pod with no network access.
5. The release rule checks the output against the contract and removes small groups.
6. Deterministic checks and a second agent review the released output.
7. The workflow promotes the UDF if the checks pass. Otherwise it asks a person.

The agent never receives credentials for storage, the cluster, or the dataset.

## Requirements

- macOS or Linux with Docker. Give Docker 16 GB of memory.
- `kind`, `kubectl`, `cilium-cli`, `uv`, and `make`.
- An Anthropic API key, or Ollama with a local model.

Install the command line tools on macOS:

```bash
brew install kind cilium-cli uv
```

## Install

Build the cluster, images, secrets, and deployments:

```bash
make up
```

`make up` asks for an Anthropic API key. Press Enter to skip it. Everything except model
calls works without a key.

Check the platform without a model:

```bash
make smoke
```

`make smoke` calls the gateway directly. It shows an allowed run, a denied dataset, a
blocked network attempt, a rejected output, and an exhausted run budget.

## Run a local model

Ollama runs on the host, not in the cluster, so it can use the GPU.

Download the model and build a 64K context variant. This downloads about 19 GB:

```bash
make ollama
```

Point the platform at the local model:

```bash
make model-local
```

Point it back at the Anthropic API:

```bash
make model-claude
```

## Ask a question

The command line front door needs no account:

```bash
make ask Q="Which member segments have the highest average claim cost?"
```

Follow the run. The output updates every 15 seconds:

```bash
make watch ID=research-xxxxxxxxxxxx
```

Approve a run that the platform flagged for a person:

```bash
make decide ID=research-xxxxxxxxxxxx DECISION=approve AS=approver@example.com
```

`AS=` sets who you are. The file `platform/catalog.yaml` lists people and roles under
the `people` key. Cedar policies in `platform/policies/launcher.cedar` decide per person.
A person cannot approve their own request.

## Front doors

A front door is the application a person uses. Each one calls the same four tools on the
research launcher: `list_problems`, `start_research`, `research_status`, and `decide`. A
front door holds one token for the launcher. It holds no cluster access, no storage
credentials, and no gateway token.

| Front door | Command | Address | Identity | Storage |
|---|---|---|---|---|
| Command line | `make ask` | none | `AS=` argument | none |
| LibreChat | `make librechat` | http://127.0.0.1:3080 | signed-in user | MongoDB |
| Open WebUI | `make openwebui` | http://127.0.0.1:8080 | signed-in user | SQLite file |
| ZeroClaw | `make zeroclaw` | http://127.0.0.1:42617 | one operator, set in config | none |
| OpenClaw | `make ui` | http://127.0.0.1:18789 | one operator | files and SQLite |

`docs/front-doors.md` compares the five front doors.

LibreChat and Open WebUI support accounts. Create the first account in the web page.
ZeroClaw requires a pairing code. Print the current code:

```bash
make zeroclaw-pair
```

OpenClaw uses a second path. It starts a Claude Code pod over the Agent Client Protocol
(ACP) instead of calling the launcher. Its configuration is `openclaw/openclaw.json`.

## Approval and loop limits

Each problem file sets its own limits:

```yaml
loop:
  max_sessions: 4
  max_hours: 10
approval:
  mode: auto_if_clean
  reviewer: true
max_runs: 5
```

- `max_sessions`: how many times the agent restarts with feedback.
- `max_hours`: wall clock limit for the whole run.
- `max_runs`: real runs per session. The gateway enforces this limit.
- `approval.mode`: `human` asks a person every time. `auto_if_clean` promotes when all
  checks pass and asks a person otherwise. `auto` always promotes.
- `approval.reviewer`: run the reviewer agent.

The platform records an automatic promotion as `policy:auto_if_clean`.

## What the platform checks

The release rule is `platform/gateway/release.py`. It rejects output when:

- Columns, their order, or their types do not match the contract.
- A group size differs from the count the platform computes from the data.
- A value differs from a metric the platform recomputes, listed under `verify`.
- A group does not exist in the data.
- The output has more rows or bytes than the contract allows.

The release rule removes groups smaller than `min_group_size.k` before release.

Deterministic checks are `platform/gateway/checks.py`. They flag calls to `__import__`,
`eval`, or `getattr`; references to `os` or `socket`; a bare `except`; long lists of
numeric literals; and a column with one repeated value.

The reviewer agent runs in a pod with no tools and no gateway token. Its only network
access is the model. It returns a risk level, a list of flags, and whether the output
answers the question. It can send a run to a person. It cannot approve one.

## Watch a run

| View | Command | Address | Content |
|---|---|---|---|
| Dashboard | `make dash` | http://127.0.0.1:8501 | Decisions, runs, released output, pods |
| Network | `make hubble` | http://127.0.0.1:12000 | Traffic between pods, including blocked traffic |
| Workflows | `make temporal` | http://127.0.0.1:8233 | Every step of every research run |
| Gateway log | `make audit` | terminal | One line per decision |
| Pods | `make pods` | terminal | Agent pods and job pods |

Hubble holds about 90 seconds of history. Open it before the event you want to see.
Produce a blocked connection on demand:

```bash
make drop-demo
```

## Add a problem

A problem is one file in `platform/contracts/`. It sets the question, the datasets, the
limits, and the output contract:

```yaml
problem: my_problem
question: >
  The question the agent answers.
teams: [research]
datasets: [my_dataset]
artifact:
  file: result.csv
  columns:
    - { name: group, type: str }
    - { name: n_members, type: int }
  group_by: [group]
  min_group_size: { column: n_members, k: 25 }
  max_rows: 500
  max_bytes: 131072
```

Register the dataset in `platform/catalog.yaml`. A dataset entry sets a sensitivity
level, the problems that may use it, and per-team column and row filters:

```yaml
datasets:
  my_dataset:
    sensitivity: internal
    problems: [my_problem]
    grants:
      research:
        columns: [group, value]
        row_filter: "region <> 'EU'"
```

`platform/gateway/lake.py` generates the synthetic dataset. Replace that file to read
your own Parquet files. Apply changes:

```bash
make images deploy
```

## Components

| Path | Component | Function |
|---|---|---|
| `platform/gateway/` | Gateway | Authorizes tool calls, copies data, runs jobs, applies the release rule |
| `platform/policies/` | Policies | Cedar rules for the gateway and the launcher |
| `platform/launcher/` | Launcher | The four front-door tools, the workflow, the agent pods |
| `platform/workers/` | Workers | UDF promotion and notifications |
| `platform/contracts/` | Problems | One file per research problem |
| `harness/claude/` | Agent image | Claude Code and its instructions |
| `udf-runner/` | Job image | Runs one UDF with no network |
| `frontdoors/` | Front doors | Configuration for LibreChat, Open WebUI, and ZeroClaw |
| `k8s/` | Deployment | Namespaces, access rules, network policies, workloads |
| `dash/` | Dashboard | Read-only view of decisions and runs |

## Cloud equivalents

gija uses local stand-ins for managed services. The code calls storage through `boto3`
with `AWS_ENDPOINT_URL`, so those calls also work against Amazon Web Services (AWS).

| Local | Managed service |
|---|---|
| moto | Amazon S3, Amazon SNS, Amazon SQS |
| Kubernetes job with no network | SageMaker Processing with network isolation |
| DuckDB filtered copy | Athena or Glue under Lake Formation grants |
| Temporal development server | Temporal Cloud or AWS Step Functions |
| Cedar policies in the gateway | Amazon Verified Permissions |

## Limits

- The dataset is synthetic. gija has not been tested on real data.
- Agent pods can resolve domain names, so data can leave through DNS queries. Add a DNS
  firewall or an egress proxy before using real data.
- A failure message from a UDF reaches the agent, truncated to 300 characters.
- Every agent pod calls the gateway as one identity, `research-bot`. Per-person identity
  stops at the launcher.
- An account that can create pods in the `agents` namespace can read that namespace's
  secrets.
- The storage stand-in enforces no access control. Network policy restricts which pods
  reach it.
- The reviewer agent is a language model. Deterministic checks must also pass before an
  automatic promotion.
- An agent session lives in its pod. A restarted session starts again from the question.
- MongoDB is pinned to version 7.0. Version 8 does not start on Linux kernel 6.19 to
  7.0.13 ([SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)).

## Tests

The offline tests cover the policies, the release rule, the checks, and the workflow:

```bash
make test
```

## Remove

Delete the cluster and everything in it:

```bash
make down
```

## License

MIT. See `LICENSE`.
