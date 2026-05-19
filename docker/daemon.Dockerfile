# serve-engine daemon as a container.
#
# Build:  docker build -f docker/daemon.Dockerfile -t serve-engine:dev .
# Run:    docker run -d --name serve --network host \
#             -v ~/.serve:/root/.serve \
#             -v /var/run/docker.sock:/var/run/docker.sock \
#             serve-engine:dev
#
# Note: --network host is the simplest way to let the daemon spawn sibling
# engine containers and reach them by 127.0.0.1:<host_port>. With a bridge
# network, the daemon container would need the engine containers on the
# same custom network and would address them by container name.

FROM python:3.12-slim AS base

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -s /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /opt/serve-engine
COPY pyproject.toml ./
COPY src ./src
COPY README.md ./

RUN uv pip install --system --no-cache .

EXPOSE 11500
VOLUME ["/root/.serve"]

CMD ["python3", "-m", "berth.daemon", "--host", "0.0.0.0", "--port", "11500"]
