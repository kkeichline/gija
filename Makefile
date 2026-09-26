# Local prototype: OpenClaw 2.x as the coordination layer over a governed MCP gateway.
# `make help` lists targets; `make up` builds everything.

CLUSTER := openclaw
IMAGES  := platform:0.1 udf-runner:0.1 harness-claude:0.1 openclaw-k8s:0.1
KUBECTL := kubectl --context kind-$(CLUSTER)

.DEFAULT_GOAL := help
.PHONY: help up cluster images secrets deploy ollama model-local model-claude smoke agent-check problems ask status watch decide temporal librechat openwebui ui dash hubble drop-demo audit logs pods test down

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

up: cluster images secrets deploy ## Create everything (prompts for your API key)

cluster: ## kind cluster + Cilium + the shared run directory
	@kind get clusters | grep -qx $(CLUSTER) || kind create cluster --name $(CLUSTER) --config cluster/kind.yaml
	@cilium status --context kind-$(CLUSTER) >/dev/null 2>&1 || cilium install --context kind-$(CLUSTER) --wait
	@cilium status --context kind-$(CLUSTER) --wait >/dev/null
	docker exec $(CLUSTER)-control-plane sh -c 'mkdir -p /var/local/runs && chown 10001:10001 /var/local/runs'

images: ## Build the four images and load them into kind
	docker build -t platform:0.1 platform/
	docker build -t udf-runner:0.1 udf-runner/
	docker build -t harness-claude:0.1 harness/claude/
	docker build -t openclaw-k8s:0.1 openclaw/
	kind load docker-image --name $(CLUSTER) $(IMAGES)

secrets: ## Create Secrets; prompts for your Anthropic API key (hidden, never written to disk)
	@scripts/secrets.sh

deploy: ## Apply manifests and config, then wait for everything to be ready
	$(KUBECTL) apply -f k8s/00-namespaces.yaml
	$(KUBECTL) create configmap openclaw-config -n openclaw --from-file=openclaw/openclaw.json --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) create configmap acp-shim -n openclaw --from-file=openclaw/acp-shim/ --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) create configmap librechat-config -n librechat --from-file=frontdoors/librechat/librechat.yaml --dry-run=client -o yaml | $(KUBECTL) apply -f -
	@scripts/model.sh ensure
	$(KUBECTL) apply -f k8s/
	$(KUBECTL) rollout restart deploy -n gateway mcp-gateway
	$(KUBECTL) rollout restart deploy -n aws-sim udf-mover
	$(KUBECTL) rollout restart deploy -n openclaw openclaw
	$(KUBECTL) rollout status deploy -n aws-sim moto --timeout=3m
	$(KUBECTL) rollout status deploy -n gateway mcp-gateway --timeout=3m
	$(KUBECTL) rollout status deploy -n openclaw openclaw --timeout=6m
	$(KUBECTL) rollout restart deploy -n launcher launcher
	$(KUBECTL) rollout status deploy -n temporal temporal --timeout=3m
	$(KUBECTL) rollout status deploy -n launcher launcher --timeout=3m
	$(KUBECTL) rollout restart deploy -n librechat librechat
	$(KUBECTL) rollout status deploy -n librechat librechat --timeout=5m
	$(KUBECTL) rollout restart deploy -n openwebui openwebui
	$(KUBECTL) rollout status deploy -n openwebui openwebui --timeout=5m

ollama: ## Install Ollama on this Mac and build the local model (one-time, ~19 GB)
	@command -v ollama >/dev/null || brew install ollama
	@brew services list | grep -qE '^ollama +started' || brew services start ollama
	ollama pull qwen3-coder:30b
	ollama create qwen3-coder-64k -f ollama/Modelfile

model-local: ## Use the local Ollama model for the Coordinator and Claude Code (no API spend)
	@scripts/model.sh local

model-claude: ## Switch back to Claude via the Anthropic API
	@scripts/model.sh claude

smoke: ## Play the agent's role (no model needed): allowed, denied, sandboxed, suppressed, budget
	@$(KUBECTL) delete pod smoke -n agents --ignore-not-found >/dev/null
	$(KUBECTL) run smoke -n agents --image platform:0.1 --image-pull-policy Never --restart Never \
	  --labels app=acp-harness --overrides "$$(cat tests/smoke-pod.json)" --stdin --attach --rm --quiet

agent-check: ## One real Claude Code turn in a harness pod (no OpenClaw): model -> governed MCP -> job -> release
	@$(KUBECTL) delete pod agent-check -n agents --ignore-not-found >/dev/null
	$(KUBECTL) run agent-check -n agents --image harness-claude:0.1 --image-pull-policy Never --restart Never \
	  --labels app=acp-harness --overrides "$$(cat tests/agent-check-pod.json)" --attach --rm --quiet

RESEARCH := uv run -q --no-project --with mcp==2.2.0 python scripts/research.py
# `AS` (who you are) shadows make's built-in assembler variable, so set it unless given.
ifeq ($(origin AS),default)
AS := researcher@example.com
endif

problems: ## Front door (CLI): what can be asked                          [AS=you@example.com]
	@$(RESEARCH) problems --as "$(AS)"

ask: ## Front door (CLI): start research, e.g. make ask Q="Which segments cost most?"
	@test -n "$(Q)" || { echo 'usage: make ask Q="your question" [AS=you@example.com]'; exit 2; }
	@$(RESEARCH) ask --as "$(AS)" --question "$(Q)"

status: ## Front door (CLI): research status and released result      ID=research-…
	@$(RESEARCH) status --as "$(AS)" --id "$(ID)"

watch: ## Front door (CLI): follow research until it needs a decision  ID=research-…
	@$(RESEARCH) watch --as "$(AS)" --id "$(ID)"

decide: ## Front door (CLI): approve/reject promotion   ID=… DECISION=approve AS=approver@example.com
	@$(RESEARCH) decide --as "$(AS)" --id "$(ID)" --decision "$(or $(DECISION),approve)"

openwebui: ## Open WebUI front door at http://127.0.0.1:8080 (first account becomes admin)
	$(KUBECTL) port-forward -n openwebui svc/openwebui 8080:8080

librechat: ## LibreChat front door at http://127.0.0.1:3080 (register any email; approver@example.com approves)
	$(KUBECTL) port-forward -n librechat svc/librechat 3080:3080

temporal: ## Temporal UI (every research run's history) at http://127.0.0.1:8233 (Ctrl-C to stop)
	$(KUBECTL) port-forward -n temporal svc/temporal 8233:8233

ui: ## Control UI at http://127.0.0.1:18789 (prints the token; Ctrl-C to stop)
	@echo "Gateway token: $$($(KUBECTL) get secret openclaw-secrets -n openclaw -o jsonpath='{.data.OPENCLAW_GATEWAY_TOKEN}' | base64 -d)"
	$(KUBECTL) port-forward -n openclaw deploy/openclaw 18789:18789

dash: ## Governance dashboard at http://127.0.0.1:8501 (Streamlit on this Mac; Ctrl-C to stop)
	uv run -q --no-project --with streamlit==1.63.0 streamlit run dash/dash.py \
	  --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false

hubble: ## Hubble UI: live network flows, incl. blocked traffic, at http://127.0.0.1:12000 (Ctrl-C to stop)
	@$(KUBECTL) get deploy hubble-ui -n kube-system >/dev/null 2>&1 || cilium hubble enable --ui --context kind-$(CLUSTER)
	$(KUBECTL) port-forward -n kube-system svc/hubble-ui 12000:80

drop-demo: ## Make a pod in the UDF sandbox try DNS and the internet (watch it get dropped in Hubble)
	@$(KUBECTL) delete pod drop-demo -n jobs --ignore-not-found >/dev/null
	$(KUBECTL) run drop-demo -n jobs --image busybox:1.37 --restart Never \
	  --overrides "$$(cat tests/drop-demo-pod.json)" --attach --rm --quiet

audit: ## Follow the gateway's decision log (every Cedar allow/deny, run, and release)
	$(KUBECTL) logs -n gateway deploy/mcp-gateway -f --tail=50

logs: ## Follow OpenClaw's gateway log
	$(KUBECTL) logs -n openclaw deploy/openclaw -c gateway -f --tail=100

pods: ## Watch harness pods and UDF jobs come and go
	$(KUBECTL) get pods -A -l 'app in (acp-harness,udf-job)' -w

test: ## Offline tests for policies, snapshots, and the release rule
	uv run -q --no-project --with-requirements platform/requirements.txt --with pytest==8.4.2 \
	  --with pytest-asyncio==1.4.0 pytest -q -o asyncio_mode=auto tests/

down: ## Delete the cluster
	kind delete cluster --name $(CLUSTER)
