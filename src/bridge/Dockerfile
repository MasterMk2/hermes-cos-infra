FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates curl python3 python3-venv nodejs npm openssh-client \
 && rm -rf /var/lib/apt/lists/*

# NOTE (F.1.1-Pi): real Coding Agent CLIs (claude-code / opencode / codex) are
# installed in a later image revision (1.0.1+) once the user selects the agent
# and provides its own authentication. The fixture pipeline needs none.

RUN useradd -m -u 1000 worker

WORKDIR /workspace
