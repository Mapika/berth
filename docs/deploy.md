# Deploying serve-engine on a public VPS

Operator guide for standing up the leader on a small Linux VPS with a
public DNS name, Caddy as the TLS-terminating front, and one or more
remote GPU hosts joining as agents.

The `serve deploy bootstrap` command (see [Bootstrap](#bootstrap) below)
automates most of this. This page documents the underlying recipe so
you can run it manually or audit what the bootstrap does.

## Requirements

**VPS**:
- 2 vCPU / 2 GB RAM is plenty (the leader doesn't run inference).
- ~5 GB disk for db / ca / logs.
- Public IPv4 (or IPv6).
- Ports 80 and 443 open inbound (for ACME + the OpenAI endpoint via
  Caddy). Port 11501 open inbound for agents to dial the cluster
  listener.
- Outbound to the agents' GPU hosts is *not* required — agents always
  dial the leader.

**DNS**:
- One A record (or AAAA) pointing at the VPS, e.g. `serve.example.com`.
  Caddy uses this name to fetch the Let's Encrypt cert.

**Agents** (the GPU hosts):
- Outbound reachability to `serve.example.com:11501`. No inbound
  needed — useful behind corporate VPNs that block client-to-client
  traffic.

## Topology

```
[ public internet ]
        ↓
   serve.example.com   (Caddy: ACME, rate limit, reverse proxy)
        ↓ 127.0.0.1:11500
   serve-engine daemon (public listener; http://, behind proxy)
        ↑ 11501 (mTLS WS)
   ──┬───────────────────
     │ outbound TLS
     ↓
   GPU host A    GPU host B    …
   serve-engine agent (no inbound exposure)
```

## Bring-up (manual)

1. **VPS bootstrap** — fresh Ubuntu/Debian, as a non-root user with sudo:

   ```bash
   # System prep
   sudo useradd -r -m -d /var/lib/serve serve
   sudo apt-get install -y python3.13-venv docker.io
   sudo usermod -aG docker serve
   ```

2. **Install Caddy**:

   ```bash
   sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
   sudo apt update && sudo apt install -y caddy
   ```

3. **Drop the Caddyfile** (see [caddy.md](caddy.md)). Point Caddy at
   `serve.example.com` → `http://127.0.0.1:11500`. ACME will fetch a
   cert on first start.

4. **Install serve-engine**:

   ```bash
   sudo -u serve mkdir -p /opt/serve
   sudo -u serve python3 -m venv /opt/serve/venv
   sudo -u serve /opt/serve/venv/bin/pip install /path/to/serve-engine
   ```

5. **Configure** `/var/lib/serve/.serve/config.toml`:

   ```toml
   [public]
   host = "serve.example.com"        # advertised in enrollment URIs
   port = 11500                      # internal port behind Caddy
   bind = "127.0.0.1"                # Caddy is on the same box
   scheme = "http"                   # Caddy terminates TLS
   trust_proxy_headers = true
   forwarded_allow_ips = "127.0.0.1"

   [cluster]
   bind = "0.0.0.0"                  # agents dial in from elsewhere
   port = 11501
   ```

6. **Install the systemd unit** (`packaging/serve-engine.service` in
   this repo):

   ```bash
   sudo cp packaging/serve-engine.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now serve-engine
   sudo systemctl status serve-engine    # confirm active (running)
   ```

7. **Mint the first admin key** over the UDS (auth-bypassed for the
   first key only):

   ```bash
   sudo -u serve /opt/serve/venv/bin/serve keys create --tier admin --name root
   # ← prints sk-… once. Save it somewhere safe.
   ```

8. **Enroll a GPU agent**:

   ```bash
   # On the leader:
   sudo -u serve /opt/serve/venv/bin/serve nodes enroll gpu-host-1
   # → emits a serve://enroll?leader=…&token=…&ca_fp=… URI

   # On the GPU host:
   serve agent register --uri '<paste>'
   serve agent start                # or run under systemd similarly
   ```

## Bootstrap

`serve deploy bootstrap` (planned) wraps steps 4–7 into a single
script. Until that lands, use the manual recipe above.

## Backup and DR

`serve backup create /var/backups/serve-$(date +%F).tar.gz` snapshots
the recoverable state:

- `db.sqlite` (consistent via SQLite `.backup`)
- `ca/` (CA cert + private key — losing this invalidates every agent
  cert)
- `key_pepper` (losing this invalidates every API key)
- `config.toml`

**Not in the backup** (deliberately):
- Model weights (`models/`) — large; re-downloadable from HF.
- Logs.

Run on a cron and copy off-host. The CA private key is the
keys-to-the-kingdom — treat the tarball like a credential.

## Operational tips

- Use `/readyz` (not `/healthz`) for load-balancer health probes —
  `/healthz` returns 200 unconditionally; `/readyz` blocks until
  startup is complete and the DB is reachable.
- `/metrics` requires a non-revoked API key (any tier) when keys exist.
- Per-IP rate limiting respects `X-Forwarded-For` when
  `trust_proxy_headers = true` — make sure Caddy strips spoofed
  inbound XFF headers before forwarding (`request_header -X-Forwarded-For`
  in the Caddyfile if you want full control).
- The CA directory should be excluded from any backup tool that
  doesn't already encrypt at rest.
