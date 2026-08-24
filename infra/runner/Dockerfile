# ArenaBench trial runner.
#
# One container = one trial slot on AWS Batch: it boots its own dockerd
# (privileged docker-in-docker, so harbor task containers are fully isolated
# per trial), stages a prebuilt SUT binary the way sut_build.py would, and
# runs `arenabench run` against a match TOML fetched from S3.
#
# Build context is the repository root. PKG_PATH locates the ArenaBench
# package inside it: "." for this standalone repository; a fork that vendors
# ArenaBench into a subdirectory passes that subdirectory.
ARG PKG_PATH=.

# Harbor is the benchmark harness ArenaBench drives; without it `arenabench
# run` dies at launch with `harbor is not on PATH` and no trial ever starts.
# 0.6.1 is Terminal-Bench 2.1's audited constant — the version every
# published claim was measured on — so it is pinned, not floated. Datasets
# needing a newer Harbor (Frontier-Bench declares min_harbor for exactly this
# reason) are not runnable in this image yet; see the issue referenced in the
# infra README.
ARG HARBOR_VERSION=0.6.1

# The Stella seat's Harbor adapter, which lives in the Stella repository
# (bench/harbor_adapter) — this repo no longer carries a copy, so it is
# fetched at build time from STELLA_GIT_URL at STELLA_ADAPTER_REF (a branch
# or tag; `git clone --branch` does not take a bare commit SHA). Its absence
# is silent and lethal: harbor_agent.py falls back to `_Base = object` and
# Harbor dies with `ArenaStellaAgent() takes no arguments` — while exiting
# 0, so the seat reports "done" having run nothing. That is why the fetch
# ends in an import assertion that fails the *build* instead of the match.
ARG STELLA_GIT_URL=https://github.com/macanderson/stella.git
ARG STELLA_ADAPTER_REF=main

# ECR Public mirror of the official image: Docker Hub rate-limits anonymous
# pulls from CodeBuild's shared egress IPs (429s), ECR Public does not.
FROM public.ecr.aws/docker/library/ubuntu:24.04
ARG PKG_PATH
ARG HARBOR_VERSION
ARG STELLA_GIT_URL
ARG STELLA_ADAPTER_REF
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq unzip gnupg procps \
        iptables util-linux \
        python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Docker engine from the official repository — the trial hosts its own
# dockerd, so harbor's `docker compose` task stacks work unmodified.
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Node 22 + Claude Code: the required day-1 comparison agent.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 for S3 artifact sync, SSM credential fetch, DynamoDB status.
RUN curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip

# ArenaBench itself. Stdlib-only by contract, so this install is dependency
# free; the pre-built web client ships inside the package.
COPY ${PKG_PATH}/pyproject.toml /opt/arenabench/pyproject.toml
COPY ${PKG_PATH}/README.md /opt/arenabench/README.md
COPY ${PKG_PATH}/LICENSE /opt/arenabench/LICENSE
COPY ${PKG_PATH}/arenabench /opt/arenabench/arenabench
RUN python3 -m pip install --break-system-packages /opt/arenabench

# Harbor: the harness that actually runs the tasks. Installed after
# ArenaBench so a Harbor version bump does not invalidate the layers above.
RUN python3 -m pip install --break-system-packages "harbor==${HARBOR_VERSION}" \
    && harbor --version

# The Stella seat's Harbor adapter (`stella_harbor`), which `arenabench`'s
# ArenaStellaAgent subclasses. A sparse, blobless, depth-1 clone fetches
# only bench/harbor_adapter from the Stella repository; installed as a
# package rather than dropped on PYTHONPATH so a missing import fails the
# *build* instead of the match. Note this layer floats with the ref: rebuild
# the image (arenabench-runner-image) to pick up adapter changes.
RUN git clone --depth=1 --filter=blob:none --sparse \
        --branch "${STELLA_ADAPTER_REF}" "${STELLA_GIT_URL}" /tmp/stella-src \
    && git -C /tmp/stella-src sparse-checkout set bench/harbor_adapter \
    && cp -a /tmp/stella-src/bench/harbor_adapter /opt/stella-harbor \
    && rm -rf /tmp/stella-src \
    && python3 -m pip install --break-system-packages /opt/stella-harbor \
    && python3 -c "import stella_harbor"
# The runner resolves the adapter *source* through this variable (or a Stella
# checkout, which the container deliberately does not carry): `stage_adapter`
# copies it content-addressed per trial (#2127). The install above proves the
# import; this names the directory the staging reads.
ENV ARENABENCH_STELLA_ADAPTER=/opt/stella-harbor

COPY ${PKG_PATH}/infra/runner/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["smoke"]
