FROM node:24-bookworm-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 \
    && npm install --global @openai/codex@0.145.0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
ENTRYPOINT ["codex"]
