# Harness pod image: Claude Code behind the ACP adapter, spoken to over stdio by OpenClaw's shim.
FROM node:24-bookworm-slim
RUN npm install -g @agentclientprotocol/claude-agent-acp@0.76.0 && npm cache clean --force
COPY --chown=node:node workspace/ /workspace/
USER node
# OpenClaw's default workspace path, in case a spawn path sends it as the cwd.
RUN mkdir -p /home/node/.openclaw && ln -s /workspace /home/node/.openclaw/workspace
# Pre-accept the workspace trust prompt; otherwise headless Claude Code ignores the
# baked .claude/settings.json permissions. The image is the only thing this trusts.
RUN printf '{"projects":{"/workspace":{"hasTrustDialogAccepted":true},"/home/node/.openclaw/workspace":{"hasTrustDialogAccepted":true}}}\n' \
    > /home/node/.claude.json
WORKDIR /workspace
# Egress is limited to api.anthropic.com anyway; this stops Claude Code from trying anything else.
ENV DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENTRYPOINT ["claude-agent-acp"]
