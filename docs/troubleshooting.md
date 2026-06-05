# Troubleshooting

When something is wrong, start here:

```bash
berth doctor
berth status
berth ps
berth logs
```

`berth doctor` checks the host: the `~/.berth` directory, port 11500, Docker,
GPUs, your Hugging Face token, and whether the engine images are already
pulled. `status` and `ps` tell you what's deployed and how it's doing. `logs`
streams the active engine container.

Almost every problem is one of five things: Docker can't reach the GPU, the
NVIDIA runtime isn't set up, Hugging Face auth, TLS, or an engine that never
got healthy. The sections below are roughly in the order you'll hit them.

## Docker can't see the GPU

Symptoms:

- `berth doctor` warns about Docker GPU access.
- Engine containers start and then die immediately.
- Docker errors mention `nvidia`, `device_requests`, or CDI.

Check it directly:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
docker info | grep -i nvidia
```

If the first command can't see the GPUs, berth can't either. Note that Docker
28 and up uses CDI and no longer lists an `nvidia` runtime in `docker info`, so
an empty grep isn't proof of anything by itself. `berth doctor` accounts for
this: it falls back to actually launching a throwaway GPU container.

Fix:

- Install or repair `nvidia-container-toolkit`.
- Restart the Docker daemon.
- Re-run `berth doctor`.

If `docker info` itself fails or you get permission errors, your user probably
isn't in the `docker` group.

## The browser warns about the certificate

On first run, when you haven't set `[public_tls]`, berth serves the public
endpoint with a cert from its own local CA. Browsers and SDKs won't trust that
by default, and that's expected.

For quick local testing, skip verification:

```bash
curl -k https://127.0.0.1:11500/healthz
```

For anything you actually expose, get a real cert. Bootstrap behind a reverse
proxy:

```bash
berth deploy bootstrap --domain berth.example.com
```

That writes a behind-Caddy config and prints a ready-to-paste Caddyfile. Put
Caddy or Nginx in front and let it handle TLS. See [caddy.md](caddy.md).

## Model download fails

Symptoms:

- `berth pull` fails with a 401, 403, or a gated-model message.
- The model exists on Hugging Face but won't fetch.

Set a token and pull again:

```bash
export HF_TOKEN=hf_...
berth pull owner/repo --name local-name
```

For private or gated repos, the token has to belong to an account that's been
granted access to that repo. `berth doctor` warns when `HF_TOKEN` is unset, but
it can't tell you whether the token has the right grants. (Either `HF_TOKEN` or
`HUGGING_FACE_HUB_TOKEN` works.)

## Engine never becomes healthy

Symptoms:

- The deployment sits at `loading`, then flips to `failed`.
- `berth logs` shows engine startup errors.

Look at what the engine is saying:

```bash
berth ps
berth logs
docker ps -a --filter name=berth-
```

Usual causes:

- Wrong engine image tag.
- The model doesn't fit at the requested context and concurrency.
- An engine-specific launch flag is invalid.
- The Hugging Face download is incomplete.

The fastest thing to try is asking for less. Drop the context and the
concurrency:

```bash
berth run qwen-0_5b --gpu 0 --engine vllm --ctx 1024 --max-seqs 4
```

`--max-seqs` caps concurrent decode sequences (vLLM `--max-num-seqs`, SGLang
`--max-running-requests`) and feeds KV-pool sizing. Lower it hard for
hybrid/Mamba models, where every sequence holds a state-cache block. If a
specific engine flag is the problem, pass raw flags with `--extra` (repeatable),
e.g. `-x '--reasoning-parser=qwen3'`.

## Placement refuses to start

Usually this is on purpose. berth estimated the deployment wouldn't fit beside
what's already loaded, so it failed the launch instead of OOMing the host.

See what's taking the room, free some, and retry:

```bash
berth ps
berth stop <deployment-id>
berth run <model> --gpu 0 --ctx 2048 --max-seqs 8
```

Pinned deployments are never auto-evicted. If the thing in the way is pinned,
unpin it (by model name) or stop it (by id) first:

```bash
berth unpin <model-name>
berth stop <deployment-id>
```

You can also just ask for a smaller footprint with `--ctx` and `--max-seqs`,
same as the section above.

## Port or listener confusion

There are three listeners, and mixing them up is a classic. Defaults:

- Public API and UI: `https://<public_host>:11500`
- Cluster agent listener (mTLS): `https://<cluster_host>:11501`
- Local CLI control socket: `~/.berth/sock`

The CLI talks to the daemon over the local socket, not over the network. The
`:11500` listener is for clients (OpenAI-compatible API, the UI). The `:11501`
listener is only for enrolled agents.

See what actually resolved, and where each value came from:

```bash
berth config show
```

For local-only testing, bind the public listener to loopback:

```bash
BERTH_PUBLIC_BIND=127.0.0.1 berth daemon start
```

For reverse-proxy mode, see [caddy.md](caddy.md) or bootstrap one:

```bash
berth deploy bootstrap --domain berth.example.com
```

If `berth daemon start` warns that the cluster listener is on `0.0.0.0` and
internet-reachable, set `[cluster] bind` in `~/.berth/config.toml` to a
private or VPN interface. More on the cluster side in
[multi-node.md](multi-node.md).

## API key fails

Keys can only be minted over the local control socket, so you create them from
the box itself. Tiers are `admin`, `standard`, and `trial`:

```bash
berth key create web --tier admin
```

For the packaged systemd layout, the `/usr/local/bin/berth` wrapper runs as the
service user against the `/var/lib/berth` state directory, so create keys
through that wrapper.

The secret is printed once. Then point a client at it:

```bash
export BERTH_TOKEN=sk-...
export BERTH_URL=https://127.0.0.1:11500
curl -k "$BERTH_URL/v1/models" -H "Authorization: Bearer $BERTH_TOKEN"
```

If a key that used to work suddenly fails everywhere, check that
`~/.berth/key_pepper` is still there. Keys are verified against that file. Lose
it and no existing key can be verified again — you'd have to mint new ones. So
back it up together with `db.sqlite`. The easiest way is one tarball of the
whole recoverable set (db, CA, pepper, config):

```bash
berth backup create /var/backups/berth-$(date +%F).tar.gz
```
