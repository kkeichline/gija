#!/usr/bin/env bash
# Creates the prototype's Secrets. The Anthropic API key is read without echo and sent
# straight to the cluster (via a file descriptor, not argv); it never touches disk.
# Re-running keeps the existing gateway, hook, and agent tokens.
set -euo pipefail

key="${ANTHROPIC_API_KEY:-}"
if [[ -z "$key" ]]; then
  read -rsp "Anthropic API key (input hidden; press Enter to skip for now): " key || true
  echo
fi
if [[ -z "$key" ]]; then  # keep whatever key is already in the cluster
  key="$(kubectl get secret openclaw-secrets -n openclaw -o jsonpath='{.data.ANTHROPIC_API_KEY}' 2>/dev/null | base64 -d 2>/dev/null || true)"
fi
if [[ -z "$key" ]]; then
  key="sk-ant-placeholder"
  echo "No key given: using a placeholder. Everything except model calls will work; re-run 'make secrets' later."
elif [[ "$key" != sk-ant-* ]]; then
  echo "That doesn't look like an Anthropic API key (expected sk-ant-...)." >&2
  exit 1
fi

existing() { kubectl get secret "$2" -n "$1" -o "jsonpath={.data.$3}" 2>/dev/null | base64 -d 2>/dev/null || true; }
fresh() { openssl rand -hex 24; }
gateway_token="$(existing openclaw openclaw-secrets OPENCLAW_GATEWAY_TOKEN)"; gateway_token="${gateway_token:-$(fresh)}"
hooks_token="$(existing openclaw openclaw-secrets OPENCLAW_HOOKS_TOKEN)";     hooks_token="${hooks_token:-$(fresh)}"
agent_token="$(existing agents harness-claude AGENT_TOKEN)";                  agent_token="${agent_token:-$(fresh)}"
platform_token="$(existing gateway gateway-tokens PLATFORM_TOKEN)";           platform_token="${platform_token:-$(fresh)}"
frontdoors="$(existing launcher launcher-secrets FRONTDOOR_TOKENS)"

apply() { kubectl create secret generic "$2" -n "$1" --from-env-file=/dev/stdin --dry-run=client -o yaml | kubectl apply -f - >/dev/null; }

kubectl apply -f k8s/00-namespaces.yaml >/dev/null
printf 'ANTHROPIC_API_KEY=%s\nOPENCLAW_GATEWAY_TOKEN=%s\nOPENCLAW_HOOKS_TOKEN=%s\n' "$key" "$gateway_token" "$hooks_token" \
  | apply openclaw openclaw-secrets
printf 'ANTHROPIC_API_KEY=%s\nAGENT_TOKEN=%s\n' "$key" "$agent_token" | apply agents harness-claude
printf 'tokens.json={"%s": "research-bot"}\nPLATFORM_TOKEN=%s\n' "$agent_token" "$platform_token" | apply gateway gateway-tokens

# One token per front door (keep existing ones; add any that are missing).
frontdoors="$(python3 -c '
import json, secrets, sys
tokens = json.loads(sys.argv[1] or "{}")
for door in ("cli", "openclaw", "librechat", "openwebui", "zeroclaw"):
    if door not in tokens.values():
        tokens[secrets.token_hex(24)] = door
print(json.dumps(tokens))' "$frontdoors")"
printf 'FRONTDOOR_TOKENS=%s\nPLATFORM_TOKEN=%s\n' "$frontdoors" "$platform_token" | apply launcher launcher-secrets

# LibreChat: its own session/encryption secrets (kept across re-runs) and its front-door token.
lc() { existing librechat librechat-secrets "$1"; }
door_token() { python3 -c 'import json,sys; print(next(t for t, d in json.loads(sys.argv[1]).items() if d == sys.argv[2]))' "$frontdoors" "$1"; }
kubectl apply -f k8s/00-namespaces.yaml >/dev/null
printf 'CREDS_KEY=%s\nCREDS_IV=%s\nJWT_SECRET=%s\nJWT_REFRESH_SECRET=%s\nGIJA_FRONTDOOR_TOKEN=%s\nANTHROPIC_API_KEY=%s\n' \
  "$(v=$(lc CREDS_KEY); echo "${v:-$(openssl rand -hex 32)}")" "$(v=$(lc CREDS_IV); echo "${v:-$(openssl rand -hex 16)}")" \
  "$(v=$(lc JWT_SECRET); echo "${v:-$(openssl rand -hex 32)}")" "$(v=$(lc JWT_REFRESH_SECRET); echo "${v:-$(openssl rand -hex 32)}")" \
  "$(door_token librechat)" "$key" | apply librechat librechat-secrets

# Open WebUI: session key, and its gija tool server preconfigured (no admin clicking).
owui_secret="$(existing openwebui openwebui-secrets WEBUI_SECRET_KEY)"; owui_secret="${owui_secret:-$(fresh)}"
tool_servers="$(python3 -c '
import json, sys
print(json.dumps([{
    "url": "http://launcher.launcher.svc.cluster.local:8080",
    "path": "http://launcher.launcher.svc.cluster.local:8080/mcp",
    "type": "mcp",
    "auth_type": "bearer",
    "key": sys.argv[1],
    "config": {"enable": True},
    "info": {"name": "gija", "description": "Governed research: ask, follow, approve."},
}]))' "$(door_token openwebui)")"
printf 'WEBUI_SECRET_KEY=%s\nTOOL_SERVER_CONNECTIONS=%s\n' "$owui_secret" "$tool_servers" | apply openwebui openwebui-secrets

# ZeroClaw: just its front-door token (its config.toml reads ${GIJA_FRONTDOOR_TOKEN}).
printf 'GIJA_FRONTDOOR_TOKEN=%s\n' "$(door_token zeroclaw)" | apply zeroclaw zeroclaw-secrets

echo "Secrets applied. 'make ui' prints the Control UI token."
