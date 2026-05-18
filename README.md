# serve-engine

serve-engine is a local inference router and lifecycle manager.

It gives one OpenAI-compatible endpoint to a GPU host, then manages the
services behind it: start, stop, health check, route, observe, and clean up.
The engines still do the inference. serve-engine owns the operational layer
around them.

The opinionated part is simple: engines should be replaceable, routes should
be explicit, and a single-node GPU box should not need a full platform just to
run a few reliable model services.

## Status

Works today:

- Single-node NVIDIA hosts
- Docker-backed lifecycle for engine containers
- vLLM and SGLang tested end to end through the router on a real GPU
- TensorRT-LLM backend adapter present
- OpenAI-compatible `/v1/chat/completions`, `/v1/completions`,
  `/v1/embeddings`, and `/v1/models`
- Model registry, deployments, service profiles, and explicit route rules
- API keys, admin keys, and request/token rate limits
- Prometheus metrics, GPU stats, lifecycle events, logs, and `serve top`
- Web UI bundled into the Python package

Not the focus:

- Training
- Multi-host tensor parallelism
- Being a new inference engine
- Leading with adapters or LoRA
- Replacing Kubernetes for large fleets

## Requirements

- Linux
- NVIDIA GPU
- Docker 24+ with NVIDIA GPU access
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) recommended

## Install

From source:

```bash
git clone https://github.com/Mapika/serve-engine
cd serve-engine
uv tool install --editable .
serve doctor
```

For development:

```bash
git clone https://github.com/Mapika/serve-engine
cd serve-engine
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
serve doctor
```

Daemon in a container:

```bash
git clone https://github.com/Mapika/serve-engine
cd serve-engine
docker build -f docker/daemon.Dockerfile -t serve-engine:dev .
docker run -d --name serve \
  --network host \
  -v ~/.serve:/root/.serve \
  -v /var/run/docker.sock:/var/run/docker.sock \
  serve-engine:dev
```

The daemon container does not run inference itself. It talks to the host Docker
socket and starts separate engine containers.

If `serve` is already taken by an existing shell alias (for example
`alias serve='python -m http.server'`), invoke as `python -m serve_engine` or
`unalias serve`.

## First Run

Start the daemon:

```bash
serve daemon start
serve daemon status
```

The daemon binds to `127.0.0.1:11500` by default. Front it with a reverse
proxy if you need to expose it off-host.

Create an admin key:

```bash
serve key create web --tier admin
```

Save the printed `secret:` value:

```bash
export SERVE_TOKEN=sk-...
export SERVE_URL=http://127.0.0.1:11500
```

Open the web UI at:

```text
http://127.0.0.1:11500/
```

Paste the admin key when prompted.

Local CLI commands use the daemon Unix socket and do not need the HTTP bearer
token. TCP admin and `/v1/*` requests need a bearer token once any key exists.

## Quick Start

Register and download a small model:

```bash
serve pull Qwen/Qwen2.5-0.5B-Instruct --name qwen-0_5b
```

Start it on GPU 0:

```bash
serve run qwen-0_5b --gpu 0 --engine vllm --pin
serve ps
```

Call the OpenAI-compatible API:

```bash
curl "$SERVE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $SERVE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen-0_5b",
    "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
    "max_tokens": 8,
    "temperature": 0
  }'
```

Stop it:

```bash
serve stop
```

## Service Routes

The model commands are the fastest path for one model. Use service profiles
when you want a reusable launch definition and a stable public route.

Create a vLLM service profile:

```bash
curl -X POST "$SERVE_URL/admin/service-profiles" \
  -H "Authorization: Bearer $SERVE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "qwen-vllm",
    "model_name": "qwen-vllm",
    "hf_repo": "Qwen/Qwen2.5-0.5B-Instruct",
    "backend": "vllm",
    "gpu_ids": [0],
    "max_model_len": 1024,
    "target_concurrency": 4
  }'
```

Deploy it:

```bash
curl -X POST "$SERVE_URL/admin/service-profiles/qwen-vllm/deploy" \
  -H "Authorization: Bearer $SERVE_TOKEN"
```

Expose it as a public model name:

```bash
curl -X POST "$SERVE_URL/admin/routes" \
  -H "Authorization: Bearer $SERVE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "chat-default",
    "match_model": "chat",
    "profile_name": "qwen-vllm",
    "priority": 10
  }'
```

Call the route:

```bash
curl "$SERVE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $SERVE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "chat",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 64
  }'
```

Switch `"backend": "vllm"` to `"backend": "sglang"` for the same profile shape
on SGLang.

## Concepts

**Service**

A runnable inference process. Today that is usually a vLLM, SGLang, or
TensorRT-LLM container. Later it can be a local process or remote HTTP service.

**Service profile**

A saved launch definition: backend, image, model, args, GPU placement,
concurrency, context length, and timeout policy.

**Deployment**

A running instance of a service profile. Deployments move through `pending`,
`loading`, `ready`, `stopping`, `stopped`, and `failed`.

**Route**

A rule that maps an incoming request to a service profile. The first route type
matches the OpenAI `model` field and rewrites it to the served model name.

**Backend**

The adapter that knows how to launch a specific engine. Engine-specific argv,
ports, health paths, metrics paths, and memory headroom live behind this
interface.

**Driver**

The mechanism that starts and stops services. Docker is the current driver.
Process, remote HTTP, SSH, Slurm, or Kubernetes drivers can be added behind the
same lifecycle contract.

## CLI

```text
serve doctor              check host requirements
serve setup               first-run wizard
serve daemon start        start the daemon
serve daemon stop         stop the daemon
serve daemon status       show daemon status
serve pull <repo>         register and download model files
serve ls                  list registered models
serve run <name>          start a deployment
serve pin <name>          keep a deployment loaded
serve unpin <name>        allow idle eviction
serve ps                  list deployments
serve stop [<id>]         stop one deployment or all deployments
serve top                 terminal dashboard
serve logs                tail engine container logs
serve key create          create an API key
serve key list            list key prefixes
serve key revoke <id>     revoke a key
serve update-engines      check for newer pinned engine tags
```

Useful `serve run` options:

```text
--engine vllm|sglang|trtllm
--gpu 0
--gpu 0,1
--ctx 8192
--max-seqs 32
--idle-timeout 300
--pin
--image <image:tag>
--extra '--some-engine-flag=value'
```

A non-pinned deployment is evicted once `now - last_request_at` exceeds
`--idle-timeout` seconds (default 300).

## Architecture

```text
client or SDK
    |
    | HTTP /v1/*
    v
+------------------------------+
| serve daemon                 |
|                              |
| OpenAI-compatible API        |
| admin API                    |
| auth and limits              |
| router                       |
| lifecycle manager            |
| metrics and events           |
+------------------------------+
    |
    | Docker API
    v
+------------------------------+
| engine containers            |
|                              |
| vllm/vllm-openai             |
| lmsysorg/sglang              |
| tensorrt-llm                 |
+------------------------------+
```

Runtime choices:

- The daemon runs on the host as one async Python process.
- Engine services run in separate containers.
- Containers bind to random localhost ports.
- The router only sends traffic to ready deployments.
- State lives in SQLite under `~/.serve`.
- Engine defaults come from `src/serve_engine/backends/backends.yaml`.
- Per-host engine overrides live in `~/.serve/backends.override.yaml`.

## Files

By default, serve-engine owns `~/.serve`. Override it with `SERVE_HOME`.

```text
~/.serve/
|-- db.sqlite               models, deployments, profiles, routes, keys, usage
|-- sock                    local CLI control socket
|-- logs/
|   `-- daemon.log          daemon stdout and stderr
|-- models/                 downloaded Hugging Face model files
|   `-- models--owner--repo/snapshots/revision/
|-- configs/                per-deployment engine configs
|-- predictor.yaml          optional prewarm and prediction tuning
`-- backends.override.yaml  optional engine image and headroom overrides
```

## Operations Notes

- Run `serve doctor` before debugging anything else.
- Use `serve ps` for deployment state.
- Use `serve logs` when an engine fails to become healthy.
- Use `/admin/events` or `serve top` for lifecycle visibility.
- Use `--pin` for services that should stay loaded.
- Use `--idle-timeout` for services that should leave the GPU when quiet.
- Use service profiles when launch arguments need to be repeatable.
- Use routes when the public model name should not be tied to one backend.

## Performance Snapshot

Single H100 80 GB, Qwen2.5 0.5B and 1.5B, 512-token outputs, Poisson arrivals.

| QPS | Model and Engine | Agg TPS | TTFT p50 ms | E2E p50 ms |
|---:|---|---:|---:|---:|
| 1 | 0.5B vLLM | 355 | 25 | 1134 |
| 16 | 0.5B vLLM | 7169 | 33 | 1429 |
| 32 | 0.5B SGLang | 14751 | 68 | 1280 |
| 16 | 1.5B SGLang | 7904 | 38 | 1608 |
| 32 | 1.5B vLLM | 13377 | 128 | 2814 |

Treat these as a sanity check, not a universal benchmark. Engine version, model
family, context length, quantization, and GPU all matter.

## GPU Sharing

Two deployments can share a GPU. Before starting one, the daemon estimates its
VRAM cost from the model config (weights at the chosen dtype, KV cache for the
requested `--ctx` and `--max-seqs`, plus engine headroom) and only places it on
GPUs where that fits alongside the already-running deployments. If nothing
fits, the manager will evict non-pinned, idle deployments in LRU order to make
room; if it still cannot fit, the start fails with a placement error rather
than racing the existing services into an OOM.

## Development

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
pytest tests/unit
ruff check src/ tests/
```

UI build:

```bash
cd ui
npm install
npm run build
```

## Out Of Scope For V1

- Multi-host tensor parallel inference
- Training or fine-tuning
- Full autotuning of tensor parallelism, dtype, and context length
- Built-in TLS termination
- A Kubernetes replacement

Bind to `127.0.0.1` by default. Put a reverse proxy in front when exposing it
outside the host.

## Multi-Node (Secure-by-Default)

A leader serves the OpenAI API and admin API. Additional GPU hosts run a
thin agent that dials home over mTLS WebSocket. The leader terminates
TLS itself — no reverse proxy required — and ships with a two-listener
trust model that is safe to expose on the public internet.

See **[docs/multi-node.md](docs/multi-node.md)** for the full operator
guide (same-network and cross-network setup, public-cert configuration,
internet-exposure hardening, troubleshooting, roadmap).

## License

Apache 2.0. See [LICENSE](LICENSE).
