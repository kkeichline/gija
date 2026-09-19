# OpenClaw 2.x plus kubectl, the ACP-to-pod shim, and a pre-installed acpx plugin, so
# the running gateway never needs npm or any egress beyond the model API.
FROM ghcr.io/openclaw/openclaw:2026.9.3-slim

USER root
ARG KUBECTL_VERSION=v1.34.1
RUN set -eux; base="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/$(dpkg --print-architecture)"; \
    curl -fsSLo /usr/local/bin/kubectl "$base/kubectl"; \
    echo "$(curl -fsSL "$base/kubectl.sha256")  /usr/local/bin/kubectl" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/kubectl
COPY --chmod=0755 k8s-acp-shim /usr/local/bin/k8s-acp-shim
RUN mkdir -p /workspace /opt/openclaw-seed && chown node:node /workspace /opt/openclaw-seed

USER node
# The pod mounts a volume over ~/.openclaw, so keep a seed copy that the init container
# lays down on first boot (see k8s/50-openclaw.yaml).
RUN openclaw plugins install @openclaw/acpx@2026.9.3 \
    && rm -f ~/.openclaw/openclaw.json \
    && cp -a ~/.openclaw/. /opt/openclaw-seed/
