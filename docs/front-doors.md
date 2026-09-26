# Front doors

A front door is the application a person uses to ask gija a question. gija ships
configuration for five. Each one calls the same four operations on the research launcher:
`list_problems`, `start_research`, `research_status`, and `decide`. The launcher serves
them twice: as MCP tools at `/mcp`, and as HTTP routes described at `/openapi.json`.

A front door holds one bearer token for the launcher. It holds no cluster access, no
storage credentials, and no gateway token. Replacing a front door changes no policy, no
contract, and no data path.

## Versions in this repository

| Front door | Version | License | Source |
|---|---|---|---|
| Command line | part of gija | MIT | `scripts/research.py` |
| LibreChat | v0.8.7 | MIT | `k8s/90-librechat.yaml` |
| Open WebUI | v0.11.3 | own license, with a branding clause | `k8s/95-openwebui.yaml` |
| ZeroClaw | pinned by image digest | Apache-2.0 | `k8s/96-zeroclaw.yaml` |
| OpenClaw | 2026.9.3 | MIT | `k8s/50-openclaw.yaml` |

ZeroClaw publishes no version tags for its container image. gija pins the image by
digest.

## Comparison

| | Command line | LibreChat | Open WebUI | ZeroClaw | OpenClaw |
|---|---|---|---|---|---|
| Interface | terminal | web chat | web chat | web chat, webhook | web chat, chat platforms |
| Accounts | none | yes | yes | one operator, pairing code | one operator, token |
| Identity sent to gija | `AS=` argument | signed-in user's email | signed-in user's email | fixed value in config | fixed value in config |
| Extra containers | none | 1 (MongoDB) | none | none | 1 (notification sidecar) |
| Storage | none | MongoDB volume | SQLite volume | none | volume for state |
| Memory requested | none | 768 MB total | 512 MB | 64 MB | 1 GB |
| Preset assistants | not applicable | yes, in `frontdoors/librechat/librechat.yaml` | no | yes, in `frontdoors/zeroclaw/config.toml` | yes, in `openclaw/openclaw.json` |
| Build your own assistant | no | yes, a form in the web page | yes, a form in the web page | config file | config file |
| Scheduled runs | no | no | no | yes | yes |
| Connects to gija by | MCP | MCP | OpenAPI over HTTP | MCP | Agent Client Protocol, then MCP from the agent pod |

## How each one connects

**Command line.** `scripts/research.py` opens a port-forward to the launcher and calls
the four tools. It reads the `cli` token from the Kubernetes secret. Use it for tests
and for scripts.

**LibreChat.** `frontdoors/librechat/librechat.yaml` defines the MCP server, two preset
assistants, and a strict domain allowlist that permits the launcher only. It sends the
signed-in user's email in the `X-User` header. Its Agent Builder lets a person create
another assistant on top of the same tools without code.

**Open WebUI.** Open WebUI reads the launcher's OpenAPI description at `/openapi.json`
and calls the four operations over HTTP. It does not use MCP. The connection is
preconfigured through the `TOOL_SERVER_CONNECTIONS` environment variable, set in
`scripts/secrets.sh`. It sends the signed-in user's email in the
`X-OpenWebUI-User-Email` header when `ENABLE_FORWARD_USER_INFO_HEADERS` is true; the
launcher accepts both header names. `OFFLINE_MODE=true` is required, because Open WebUI
otherwise downloads an embedding model at startup, which the network policy blocks.

**ZeroClaw.** `frontdoors/zeroclaw/config.toml` defines the MCP server, the model, and
one agent. Tools are deny-by-default and the gateway requires a pairing code, printed by
`make zeroclaw-pair`. ZeroClaw has no accounts, so the person it acts for is a fixed
value in the configuration.

**OpenClaw.** OpenClaw does not call the launcher. It starts a Claude Code pod over the
Agent Client Protocol (ACP), using the shim in `openclaw/k8s-acp-shim`. The agent in that
pod calls the gateway directly. It needs rights to create pods in the `agents`
namespace, which the other front doors do not.

## Setup effort

Steps a person takes after `make up`, before the first question.

| Front door | Steps before the first question |
|---|---|
| Command line | none |
| Open WebUI | create the first account; turn the gija tool on; set Function Calling to Native |
| LibreChat | register an account; choose a preset assistant |
| ZeroClaw | read the pairing code with `make zeroclaw-pair`; enter it in the web page |
| OpenClaw | read the token with `make ui`; enter it in the Control UI |

Open WebUI needs both chat settings. With the gija tool off, the model answers from its
own memory and calls nothing. With Function Calling set to Default instead of Native,
the model prints the call as text instead of calling the launcher. Both settings are in
the chat's controls panel.

## Tested

The command line and Open WebUI have each completed a research run through the launcher
on the local model. OpenClaw has completed the path it uses instead: a Claude Code pod,
the gateway, and the release rule. LibreChat and ZeroClaw are deployed and configured.
No completed run is recorded for either.

## Choosing one

- **One person, one machine:** the command line, or ZeroClaw for a web page and
  scheduled runs.
- **A team with accounts:** LibreChat, because per-person identity reaches the
  launcher's policies and people can build their own assistants.
- **Smallest footprint:** ZeroClaw. One process, 64 MB, no database.
- **Familiar interface:** Open WebUI.
- **Existing OpenClaw users:** OpenClaw, with the caveat that it holds cluster
  permissions the others do not.

## Add a front door

1. Add a token name to the list in `scripts/secrets.sh`. It generates one token per
   front door.
2. Point the application at the launcher with the header `Authorization: Bearer
   <token>`. Use `http://launcher.launcher.svc.cluster.local:8080/mcp` for MCP, or
   `http://launcher.launcher.svc.cluster.local:8080/openapi.json` for HTTP.
3. Send the person's identity in `X-User` or `X-OpenWebUI-User-Email`.
4. Add the namespace to the launcher's ingress rule in `k8s/60-netpol.yaml`.
5. Add an egress policy for the new namespace. Copy one from the same file.

The launcher records the front door and the person for every call. Read the decisions:

```bash
kubectl logs -n launcher deploy/launcher -c server | grep authz
```
