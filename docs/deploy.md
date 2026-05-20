# Deploying berth on a public VPS

Operator guide for standing up a leader-only control plane on a small
Linux VPS with public DNS, Caddy for the public UI/API, HAProxy for
443/TLS passthrough routing, and one or more remote GPU hosts joining
as agents.

The `berth deploy bootstrap` command (see [Bootstrap](#bootstrap) below)
automates most of this. This page documents the underlying recipe so
you can run it manually or audit what the bootstrap does.

## Requirements

**VPS**:
- 2 vCPU / 2 GB RAM is plenty (the leader doesn't run inference).
- ~5 GB disk for db / ca / logs.
- Public IPv4 (or IPv6).
- Ports 80 and 443 open inbound. Agents use 443, which works through
  most corporate egress firewalls. The berth cluster listener stays on
  loopback (`127.0.0.1:11501`) behind HAProxy TLS passthrough.
- Outbound to the agents' GPU hosts is *not* required — agents always
  dial the leader.

**DNS**:
- Two A records (or AAAA) pointing at the VPS:
  - `leader.example.com` for the public UI/API.
  - `cluster.example.com` for agent enrollment and the mTLS WebSocket.

**Agents** (the GPU hosts):
- Outbound reachability to `cluster.example.com:443`. No inbound
  needed — useful behind corporate VPNs that block client-to-client
  traffic and non-standard outbound ports.

## Topology

```
[ public internet ]
        ↓ :443
   HAProxy TCP SNI router
      ├─ leader.example.com  → Caddy 127.0.0.1:8443
      │                         → berth public 127.0.0.1:11500
      └─ cluster.example.com → berth cluster 127.0.0.1:11501
                                (TLS passthrough; agent mTLS preserved)
```

## One-command VPS setup

Clone berth on a fresh Ubuntu/Debian VPS, make sure DNS already points
at the box, then run:

```bash
sudo ./scripts/setup-leader-vps.sh example.com
```

This derives:

```text
leader.example.com   public UI/API
cluster.example.com  agent enrollment + mTLS WebSocket
```

The script installs Caddy + HAProxy, creates the `berth` system user,
installs berth into `/opt/berth/venv`, writes a leader-only
`/var/lib/berth/config.toml`, bootstraps the DB/CA/key pepper/admin key,
writes systemd/Caddy/HAProxy configs, installs `/usr/local/bin/berth` as the
operator command, opens only `80/tcp` and `443/tcp` in UFW, and starts the
services.

After setup, use short commands:

```bash
berth status
berth nodes enroll gpu-host-1
berth key create teammate --tier admin
berth wipe   # reset local berth state and start over
```

## Bring-up (manual)

1. **VPS bootstrap** — fresh Ubuntu/Debian, as a non-root user with sudo:

   ```bash
   # System prep
   sudo useradd -r -m -d /var/lib/berth berth
   sudo apt-get install -y python3-venv caddy haproxy ufw
   ```

2. **Install Caddy**:

   ```bash
   sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
   sudo apt update && sudo apt install -y caddy
   ```

3. **Drop the Caddy + HAProxy configs** (see [caddy.md](caddy.md)).
   HAProxy owns external `:443` and routes by SNI. Caddy handles
   `leader.example.com`; berth handles `cluster.example.com` directly
   on loopback so agent client certificates are preserved.

4. **Install berth and the operator command**:

   ```bash
   sudo install -d -o berth -g berth -m 0755 /opt/berth
   sudo -u berth python3 -m venv /opt/berth/venv
   sudo -u berth /opt/berth/venv/bin/pip install /path/to/berth
   sudo install -d -m 0755 /etc/berth
   sudo tee /etc/berth/operator.env >/dev/null <<'EOF'
   BERTH_REAL=/opt/berth/venv/bin/berth
   BERTH_HOME=/var/lib/berth
   BERTH_USER=berth
   BERTH_LEADER_URL_DEFAULT=https://cluster.example.com
   EOF
   sudo install -m 0755 packaging/berth-wrapper /usr/local/bin/berth
   ```

5. **Configure** `/var/lib/berth/config.toml`:

   ```toml
   [server]
   leader_only = true

   [public]
   host = "leader.example.com"
   port = 11500
   bind = "127.0.0.1"                # Caddy is on the same box
   scheme = "http"                   # Caddy terminates TLS
   trust_proxy_headers = true
   forwarded_allow_ips = "127.0.0.1"

   [cluster]
   host = "cluster.example.com"
   bind = "127.0.0.1"                # HAProxy passes TLS through
   port = 11501                      # internal loopback port
   ```

   Set the advertised agent URL in the systemd unit:

   ```ini
   Environment=BERTH_LEADER_URL=https://cluster.example.com
   ```

6. **Install the systemd unit** (`packaging/berth.service` in
   this repo):

   ```bash
   sudo cp packaging/berth.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now berth
   sudo systemctl status berth    # confirm active (running)
   ```

7. **Mint the first admin key** over the UDS (auth-bypassed for the
   first key only):

   ```bash
   berth key create root --tier admin
   # ← prints sk-… once. Save it somewhere safe.
   ```

8. **Enroll a GPU agent**:

   ```bash
   # On the leader:
   berth nodes enroll gpu-host-1
   # → emits a berth://enroll?leader=…&token=…&ca_fp=… URI

   # On the GPU host:
   berth agent register --uri '<paste>'
   berth agent start                # compact foreground status
   berth agent logs --follow        # full agent log, if needed
   ```

## Bootstrap

`berth deploy bootstrap --domain <fqdn>` wraps steps 5 and 7 (write
config.toml + mint first admin key) plus the DB/CA/pepper init that
the daemon would otherwise do on first start. Idempotent: re-running
preserves the existing config and skips the key mint when keys
already exist.

Typical first-run on the VPS:

```bash
berth deploy bootstrap \
    --domain leader.example.com \
    --cluster-domain cluster.example.com \
    --sni-443 \
    --leader-only
```

Output includes:

- Ready-to-paste Caddy and HAProxy snippets.
- The first admin API key (shown once — save it).
- The exact systemd commands to enable + start the service.

For a direct-TLS deployment (no Caddy in front), pass `--direct-tls`.
The daemon's TLS will use the cluster-CA-signed server cert by default;
override with `[public_tls]` in `config.toml` if you want a custom
chain (Let's Encrypt minted out-of-band, an internal CA, etc.).

To reset a local leader and start over without uninstalling packages or proxy
configs:

```bash
berth wipe
```

`berth wipe` prompts before deleting state. Use `berth wipe --yes` only for
automation.

Operator-controlled sudo steps (install systemd unit, install Caddy and
HAProxy) stay manual on purpose — bootstrap doesn't write to /etc or run
package managers. Use `scripts/setup-leader-vps.sh` when you want those
steps automated.

## Backup and DR

`berth backup create /var/backups/berth-$(date +%F).tar.gz` snapshots
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
- `/metrics` requires a non-revoked API key (any tier), including during
  first-run bootstrap before any key exists.
- In the recommended 443/SNI setup, do not expose `11501/tcp` publicly.
  Only HAProxy should reach `127.0.0.1:11501`.
- Per-IP rate limiting respects `X-Forwarded-For` when
  `trust_proxy_headers = true` — make sure Caddy strips spoofed
  inbound XFF headers before forwarding (`request_header -X-Forwarded-For`
  in the Caddyfile if you want full control).
- The CA directory should be excluded from any backup tool that
  doesn't already encrypt at rest.
