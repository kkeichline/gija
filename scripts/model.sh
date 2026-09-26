#!/usr/bin/env bash
# Points gija at Claude (Anthropic API) or at a local Ollama model on this host.
# Only the model endpoint changes; the governed gateway, policies, and jobs don't.
#
#   scripts/model.sh local    # Coordinator + Claude Code use Ollama (no API spend)
#   scripts/model.sh claude   # back to Claude
#   scripts/model.sh ensure   # default to Claude if nothing is set yet (used by `make deploy`)
set -euo pipefail

LOCAL_MODEL="${LOCAL_MODEL:-qwen3-coder-64k}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-sonnet-5}"
OLLAMA_URL="http://host.docker.internal:11434" # Docker Desktop's name for this Mac

apply_cm() { # namespace name key=value...
  local ns=$1 name=$2 args=()
  shift 2
  for kv in "$@"; do args+=("--from-literal=$kv"); done
  kubectl create configmap "$name" -n "$ns" "${args[@]}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

use_claude() {
  apply_cm agents harness-model "ANTHROPIC_MODEL=$CLAUDE_MODEL"
  apply_cm openclaw openclaw-model "OPENCLAW_MODEL=anthropic/$CLAUDE_MODEL"
}

use_local() {
  curl -fsS -m 3 http://127.0.0.1:11434/api/version >/dev/null ||
    { echo "Ollama isn't running on this Mac. Run: make ollama" >&2; exit 1; }
  ollama show "$LOCAL_MODEL" >/dev/null 2>&1 ||
    { echo "Model $LOCAL_MODEL isn't built yet. Run: make ollama" >&2; exit 1; }
  # Claude Code asks for different tiers (opus/sonnet/haiku, subagents); map them all to
  # the one local model so Ollama never gets a Claude model name it doesn't have.
  apply_cm agents harness-model \
    "ANTHROPIC_BASE_URL=$OLLAMA_URL" "ANTHROPIC_AUTH_TOKEN=ollama" "ANTHROPIC_API_KEY=" \
    "ANTHROPIC_MODEL=$LOCAL_MODEL" "ANTHROPIC_DEFAULT_OPUS_MODEL=$LOCAL_MODEL" \
    "ANTHROPIC_DEFAULT_SONNET_MODEL=$LOCAL_MODEL" "ANTHROPIC_DEFAULT_HAIKU_MODEL=$LOCAL_MODEL" \
    "CLAUDE_CODE_SUBAGENT_MODEL=$LOCAL_MODEL"
  apply_cm openclaw openclaw-model "OPENCLAW_MODEL=ollama/$LOCAL_MODEL"
}

case "${1:-}" in
  local) use_local ;;
  claude) use_claude ;;
  ensure)
    kubectl get configmap openclaw-model -n openclaw >/dev/null 2>&1 || use_claude
    exit 0
    ;;
  *) echo "usage: $0 local|claude" >&2; exit 2 ;;
esac

kubectl rollout restart deploy/openclaw -n openclaw >/dev/null
kubectl rollout status deploy/openclaw -n openclaw --timeout=6m
echo "Now using $(kubectl get configmap openclaw-model -n openclaw -o jsonpath='{.data.OPENCLAW_MODEL}')." \
  "New harness pods pick it up; the restart closed any running ones."
