# Deploying berth on a public VPS

This is the operator guide for the leader: a small public VPS that serves
the OpenAI-compatible API and the UI, and that GPU agents dial back into
over mTLS. The leader is control-plane-only. No models run on it. The
weights live on the enrolled agent hosts, and the leader routes to them.

If you just want it done, run [the one-command setup](#one-command-vps-setup)
and skip the rest. The manual recipe below documents what that script does,
so you can run the steps by hand or audit them. For the agent side and how
the WebSocket tunnel works, see [multi-node.md](multi-node.md).

## What you need

The leader does no inference, so it's cheap.

- 2 vCPU / 2 GB RAM is plenty.
- ~5 GB disk for the DB, CA, and logs.
- A public IPv4 or IPv6.
- Ports 80 and 443 open inbound, nothing else. Agents connect on 443,
  which gets through most corporate egress firewalls. The berth cluster
  listener stays on loopback (`127.0.0.1:11501`) behind HAProxy doing TLS
  passthrough.
- No outbound to the GPU hosts. Agents always dial the leader, never the
  other way around.

You need two DNS records, both pointing at the VPS (A or AAAA):

- `leader.example.com` — the public UI and API.
- `cluster.example.com` — agent enrollment and the mTLS WebSocket.

Set these up *before* you run the installer. The setup script checks the
domain is well-formed but it can't make DNS resolve for you, and HAProxy
routes by the SNI hostname.

On the GPU hosts you only need outbound reachability to
`cluster.example.com:443`. Nothing listens for inbound. That's the whole
point: agents work fine behind corporate VPNs that block client-to-client
traffic and non-standard outbound ports.

## Topology

HAProxy owns external `:443` and routes by SNI. The public hostname goes
to Caddy, which terminates TLS and proxies to the daemon's loopback port.
The cluster hostname is passed straight through so the agent's client
certificate survives — Caddy never sees it.

```
[ public internet ]
        ↓ :443
   HAProxy TCP SNI router
      ├─ leader.example.com  → Caddy 127.0.0.1:8443
      │                         → berth public 127.0.0.1:11500
      └─ cluster.example.com → berth cluster 127.0.0.1:11501
                                (TLS passthrough; agent mTLS preserved)
```

The ports:

| Port | Bind | What |
| --- | --- | --- |
| 443 | public | HAProxy, SNI router |
| 80 | public | Caddy, redirects to HTTPS |
| 8443 | `127.0.0.1` | Caddy public HTTPS, behind HAProxy |
| 11500 | `127.0.0.1` | berth public API/UI, behind Caddy |
| 11501 | `127.0.0.1` | berth cluster mTLS listener, behind HAProxy passthrough |

Do not expose 11501 publicly in this setup. Only HAProxy should reach
`127.0.0.1:11501`.

## One-command VPS setup

Clone berth on a fresh Ubuntu or Debian box, confirm DNS already points at
it, then run:

```bash
sudo ./scripts/setup-leader-vps.sh example.com
```

It derives the two hostnames from the base domain:

```text
leader.example.com    public UI/API on external 443
cluster.example.com   agent enrollment + mTLS WebSocket on external 443
```

The script runs twelve steps and prints one status line each. Full output
of every step goes to `/var/log/berth-install.log`; the terminal stays
quiet unless something fails. It looks like this:

```text
  berth leader installer    example.com
  ────────────────────────────────────────────────────────────
  [ 1/12] Installing OS packages ························· ok  14s
  [ 2/12] Creating berth user and directories ·········· ok
  [ 3/12] Copying checkout to /opt/berth/src ··········· ok
  [ 4/12] Installing berth (hash-pinned deps) ·········· ok  39s
  [ 5/12] Installing operator wrapper ·················· ok
  [ 6/12] Bootstrapping config, CA, DB, key ············ ok
  [ 7/12] Writing systemd unit ························· ok
  [ 8/12] Writing Caddy + HAProxy config ··············· ok
  [ 9/12] Validating service configs ··················· ok
  [10/12] Firewall + unattended upgrades ··············· ok
  [11/12] Kernel + SSH hardening + fail2ban ············ ok
  [12/12] Starting services ···························· ok
```

If a step fails, the script stops, prints the last 20 lines of the install
log inline, and points you at the full log. Pass `-v` / `--verbose` to
stream every command live instead of logging it quietly. Pass `--force` to
overwrite an existing `config.toml`.

At the end it prints a framed summary with the first admin key (shown once),
the two URLs, and the log path:

```text
  ────────────────────────────────────────────────────────────
  ✓ berth leader is up

    Admin key  (shown once — save it now)
        sk-...

    Public UI/API     https://leader.example.com
    Agent endpoint    https://cluster.example.com

    Verify            berth status
    Enroll an agent   berth nodes enroll <label>
    Reset state       berth wipe
    Install log       /var/log/berth-install.log
```

Save the admin key now. It isn't shown again. If keys already exist (a
re-run), the summary says so and doesn't mint a new one.

### What the script actually does

In order, the twelve steps:

1. Installs OS packages: `ca-certificates`, `caddy`, `curl`, `fail2ban`,
   `git`, `haproxy`, `python3-venv`, `ufw`, `unattended-upgrades`.
2. Creates the `berth` system user (nologin, home `/var/lib/berth`, mode
   0700) and `/opt/berth`.
3. Copies your checkout to `/opt/berth/src`, excluding VCS state, caches,
   and anything that looks like a local secret (`.env*`, `*.pem`, `*.key`,
   `id_rsa*`, `*.kdbx`, editor configs).
4. Builds the venv at `/opt/berth/venv` and installs berth with hash-pinned
   deps. It uses `uv export --frozen` from `uv.lock` to produce a
   requirements file with hashes, installs it with `--require-hashes`, then
   installs the project itself with `--no-deps`. The VPS gets exactly what
   CI tested. (The script refuses to run if `uv.lock` is missing — run
   `uv lock` before deploying.)
5. Writes `/etc/berth/operator.env` and installs the `berth` wrapper to
   `/usr/local/bin/berth` so you run short commands as yourself.
6. Runs `berth deploy bootstrap --quiet` to write `config.toml`, generate
   the CA, init the DB, and mint the first admin key. (See
   [Bootstrap](#bootstrap).)
7. Writes the systemd unit at `/etc/systemd/system/berth.service`.
8. Writes `/etc/caddy/Caddyfile` and `/etc/haproxy/haproxy.cfg`.
9. Validates both with `caddy validate` and `haproxy -c` before starting
   anything.
10. Opens 22, 80, and 443 in UFW (enables UFW), and turns on unattended
    security upgrades.
11. Hardens the box: sysctl (`/etc/sysctl.d/99-berth.conf`), SSH (root login
    off, password auth off, validated with `sshd -t` before reload), and a
    fail2ban jail for sshd.
12. Starts and enables `berth`, `caddy`, and `haproxy`.

After it finishes, use the short commands. The wrapper runs berth as the
`berth` user against `/var/lib/berth` and defaults the leader URL to your
cluster host:

```bash
berth status                          # is the leader up
berth nodes enroll gpu-host-1         # mint an enrollment URI for an agent
berth key create teammate --tier admin
berth wipe                            # reset local berth state and start over
```

## Manual bring-up

The recipe the script automates, step by step. Run these on a fresh Ubuntu
or Debian box as a non-root user with sudo.

### 1. System prep

```bash
sudo useradd -r -m -d /var/lib/berth berth
sudo apt-get install -y python3-venv caddy haproxy ufw
```

### 2. Install Caddy

If `caddy` isn't already packaged for your release, add the Cloudsmith repo:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

### 3. Caddy + HAProxy config

HAProxy owns external `:443` and routes by SNI. Caddy handles
`leader.example.com`. berth handles `cluster.example.com` directly on
loopback so agent client certificates are preserved through the
passthrough. The full Caddyfile and `haproxy.cfg` are in
[caddy.md](caddy.md); `berth deploy bootstrap --sni-443` also prints
ready-to-paste versions of both.

### 4. Install berth and the operator command

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

### 5. Configure `/var/lib/berth/config.toml`

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

Set the advertised agent URL in the systemd unit so agents know where to
dial back:

```ini
Environment=BERTH_LEADER_URL=https://cluster.example.com
```

### 6. Install the systemd unit

The unit ships as `packaging/berth.service`:

```bash
sudo cp packaging/berth.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now berth
sudo systemctl status berth    # confirm active (running)
```

### 7. Mint the first admin key

The first key is minted over the local UDS with auth bypassed (first key
only):

```bash
berth key create root --tier admin
# prints sk-... once. Save it somewhere safe.
```

If you ran bootstrap, this already happened — skip it.

### 8. Enroll a GPU agent

```bash
# On the leader:
berth nodes enroll gpu-host-1
# emits a berth://enroll?leader=…&token=…&ca_fp=… URI (single-use, 10 min)

# On the GPU host:
berth agent register --uri '<paste>'
berth agent start                # compact foreground status
berth agent logs --follow        # full agent log, if needed
```

The enrollment URI bundles the leader URL, a single-use token, and the CA
fingerprint. The fingerprint pin is what stops a swapped CA from
man-in-the-middling the bootstrap. More on the agent side in
[multi-node.md](multi-node.md).

## Bootstrap

`berth deploy bootstrap` does the daemon-side setup the installer's step 6
runs, and that you'd otherwise leave to the daemon's first start: write
`config.toml`, generate the CA, init the DB (so migrations run once, under
the lock, with you at the keyboard), create the key pepper, and mint the
first admin key. It's idempotent except for the first key — a re-run keeps
the existing config (unless you pass `--force`) and skips the mint when keys
already exist.

It deliberately does *not* touch `/etc`, install packages, or write systemd
units. Those are sudo-territory and OS-specific, so bootstrap prints the
exact commands and lets you run them. Use `scripts/setup-leader-vps.sh`
when you want all of that automated too.

The typical SNI-on-443 first run:

```bash
berth deploy bootstrap \
    --domain leader.example.com \
    --cluster-domain cluster.example.com \
    --sni-443 \
    --leader-only
```

Flags worth knowing:

- `--domain` — public FQDN clients use (required).
- `--cluster-domain` — FQDN agents dial. Required with `--sni-443`.
- `--sni-443` — generate HAProxy + Caddy config so both hostnames share
  external 443 by SNI, keeping the cluster listener on loopback with TLS
  passthrough.
- `--leader-only` — write `[server] leader_only = true` for a
  control-plane-only VPS. This is what you want here.
- `--public-port` (default 11500), `--cluster-port` (default 11501),
  `--public-tls-port` (default 8443) — override the ports.
- `--berth-home` — override the daemon home (default `~/.berth`).
- `--force` — overwrite an existing `config.toml`.
- `--direct-tls` — bind the public listener with TLS directly, no Caddy in
  front. Useful when you already manage certs elsewhere. Can't be combined
  with `--sni-443`.
- `--quiet` / `-q` — print only the status block and admin key, skip the
  "Next steps" runbook. This is what the installer uses.

Without `--quiet`, the output includes ready-to-paste Caddy and HAProxy
snippets, the first admin key (once), and the exact systemd commands to
enable and start the service.

For `--direct-tls`, the daemon's TLS uses the cluster-CA-signed server cert
by default. Override it with `[public_tls]` in `config.toml` if you want a
different chain — Let's Encrypt minted out of band, an internal CA, whatever.

### Resetting

To wipe a local leader and start over without uninstalling packages or
touching the proxy configs:

```bash
berth wipe
```

It prompts before deleting state. Use `berth wipe --yes` (or `-y`) only in
automation.

## Backup and DR

This is the part that bites people. The CA private key is the
keys-to-the-kingdom: lose it and every agent certificate is dead. Treat the
backup tarball like a credential.

```bash
berth backup create /var/backups/berth-$(date +%F).tar.gz
```

That snapshots the recoverable state:

- `db.sqlite` — taken via SQLite's `.backup` so it's consistent even under
  load.
- `ca/` — the CA cert and private key. Losing this invalidates every agent
  cert.
- `key_pepper` — losing this invalidates every API key.
- `config.toml`.

Not in the backup, on purpose:

- Model weights (`models/`) — large, and re-downloadable from Hugging Face.
- Logs.

Run it on a cron and copy the tarball off-host. If your backup tool doesn't
encrypt at rest, exclude the CA directory from it and handle that one
separately.

## Operational tips

- For load-balancer health probes use `/readyz`, not `/healthz`. `/healthz`
  returns 200 unconditionally (it only proves the process is up). `/readyz`
  returns 503 until the lifespan has finished and the DB answers `SELECT 1`,
  then 200.
- `/metrics` requires a non-revoked API key of any tier — including during
  first-run bootstrap, before any key exists. Mint a key first.
- In the 443/SNI setup, never expose `11501/tcp` publicly. Only HAProxy
  reaches `127.0.0.1:11501`.
- Per-IP rate limiting honours `X-Forwarded-For` when
  `trust_proxy_headers = true`. Make sure Caddy strips spoofed inbound XFF
  before forwarding — add `request_header -X-Forwarded-For` to the Caddyfile
  if you want to be strict about it.
- `berth status` is the quick "is the leader healthy" check.

For the agent side, enrollment internals, and routing, see
[multi-node.md](multi-node.md). For the full proxy configs, see
[caddy.md](caddy.md). When something misbehaves, start with
[troubleshooting.md](troubleshooting.md).
