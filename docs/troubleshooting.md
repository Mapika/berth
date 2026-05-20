# Troubleshooting

Start with:

```bash
berth doctor
berth daemon status
berth ps
berth logs
```

Most failures are Docker, NVIDIA runtime, Hugging Face auth, TLS, or an engine
that never became healthy.

## Docker Cannot See The GPU

Symptoms:

- `berth doctor` warns about Docker GPU access.
- Engine containers start and immediately fail.
- Docker errors mention `nvidia`, `device_requests`, or CDI.

Checks:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
docker info | grep -i nvidia
```

Fix:

- Install or repair `nvidia-container-toolkit`.
- Restart Docker.
- Re-run `berth doctor`.

## Browser Warns About The Certificate

First-run berth uses a generated local CA when `[public_tls]` is not
configured. Browsers and SDKs will not trust it by default.

For quick local API testing:

```bash
curl -k https://127.0.0.1:11500/healthz
```

For a real exposed service, prefer:

```bash
berth deploy bootstrap --domain berth.example.com
```

Then put Caddy or Nginx in front. See `docs/caddy.md`.

## Model Download Fails

Symptoms:

- `berth pull` fails with a 401, 403, or gated-model message.
- The model exists on Hugging Face but cannot be fetched.

Fix:

```bash
export HF_TOKEN=hf_...
berth pull owner/repo --name local-name
```

For private or gated models, make sure the token belongs to an account with
access to the repo.

## Engine Never Becomes Healthy

Symptoms:

- Deployment stays `loading`, then moves to `failed`.
- `berth logs` shows engine startup errors.

Checks:

```bash
berth ps
berth logs
docker ps -a --filter name=berth-
```

Common causes:

- Bad engine image tag.
- Model does not fit with the requested context/concurrency.
- Engine-specific launch flag is invalid.
- Hugging Face download path is incomplete.

Try a smaller context or lower concurrency:

```bash
berth run qwen-0_5b --gpu 0 --engine vllm --ctx 1024 --max-seqs 4
```

## Placement Refuses To Start

This is usually intentional. berth estimated the requested deployment
would not fit beside what is already loaded.

Useful commands:

```bash
berth ps
berth stop <deployment-id>
berth run <model> --gpu 0 --ctx 2048 --max-seqs 8
```

Pinned deployments are not evicted automatically. Unpin or stop them first:

```bash
berth unpin <model-name>
berth stop <deployment-id>
```

## Port Or Listener Confusion

Defaults:

- Public API/UI: `https://<public_host>:11500`
- Cluster agent listener: `https://<cluster_host>:11501`
- Local CLI control socket: `~/.berth/sock`

Show resolved config:

```bash
berth config show
```

For local-only testing:

```bash
BERTH_PUBLIC_BIND=127.0.0.1 berth daemon start
```

For reverse proxy mode, use `docs/caddy.md` or:

```bash
berth deploy bootstrap --domain berth.example.com
```

## API Key Fails

Create a new admin key over the local socket:

```bash
berth key create web --tier admin
```

For the packaged systemd layout, use the daemon's home explicitly:

```bash
sudo -u berth env BERTH_HOME=/var/lib/berth \
    /opt/berth/venv/bin/berth key create web --tier admin
```

Then:

```bash
export BERTH_TOKEN=sk-...
export BERTH_URL=https://127.0.0.1:11500
curl -k "$BERTH_URL/v1/models" -H "Authorization: Bearer $BERTH_TOKEN"
```

If the key pepper file was lost from `~/.berth/key_pepper`, old keys cannot be
verified. Mint new keys and back up `db.sqlite` and `key_pepper` together.
