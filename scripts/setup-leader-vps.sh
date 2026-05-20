#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  sudo ./scripts/setup-leader-vps.sh <base-domain> [--force]

Example:
  sudo ./scripts/setup-leader-vps.sh berth.run

Creates a leader-only VPS deployment:
  leader.<base-domain>  public UI/API on external 443
  cluster.<base-domain> agent enrollment + mTLS WebSocket on external 443

Required DNS before running:
  leader.<base-domain>  A/AAAA -> this VPS
  cluster.<base-domain> A/AAAA -> this VPS
USAGE
}

die() {
  echo "error: $*" >&2
  exit 1
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

[[ $# -ge 1 ]] || { usage; exit 2; }
[[ "$(id -u)" == "0" ]] || die "run as root with sudo"

BASE_DOMAIN="$1"
shift

FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=1
      shift
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

# Strict FQDN: labels are alphanumeric (with optional internal hyphens), at
# least one dot, TLD is two or more letters. Rejects ``a..b``, leading/trailing
# hyphens, single-label inputs, and trailing dots.
if ! [[ "$BASE_DOMAIN" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$ ]]; then
  die "base domain does not look valid: $BASE_DOMAIN"
fi

PUBLIC_HOST="leader.${BASE_DOMAIN}"
CLUSTER_HOST="cluster.${BASE_DOMAIN}"
BERTH_HOME="/var/lib/berth"
BERTH_OPT="/opt/berth"
BERTH_SRC="${BERTH_OPT}/src"
BERTH_VENV="${BERTH_OPT}/venv"
PUBLIC_PORT=11500
CLUSTER_PORT=11501
CADDY_TLS_PORT=8443

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
[[ -f "${REPO_ROOT}/pyproject.toml" ]] || die "could not find repo root from ${SCRIPT_DIR}"
[[ -f "${REPO_ROOT}/uv.lock" ]] || die "uv.lock missing — run uv lock before deploying"

run_as_berth() {
  if command -v sudo >/dev/null 2>&1; then
    sudo -u berth -- "$@"
  else
    runuser -u berth -- "$@"
  fi
}

echo "==> Installing OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  ca-certificates \
  caddy \
  curl \
  fail2ban \
  git \
  haproxy \
  python3-venv \
  ufw \
  unattended-upgrades

echo "==> Creating berth user and directories"
if ! id berth >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$BERTH_HOME" --shell /usr/sbin/nologin berth
fi
install -d -o berth -g berth -m 0700 "$BERTH_HOME"
install -d -o berth -g berth -m 0755 "$BERTH_OPT"

echo "==> Copying current checkout to ${BERTH_SRC}"
# Excludes cover (a) dev caches and VCS state, and (b) common locations where
# operator-local secrets accumulate (.env, *.pem/key, editor configs, node
# modules, build artefacts). If you add a new secret-bearing pattern to a
# contributor checkout, add it here too.
rm -rf "$BERTH_SRC"
install -d -o berth -g berth -m 0755 "$BERTH_SRC"
tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.mypy_cache' \
  --exclude='.ruff_cache' \
  --exclude='node_modules' \
  --exclude='dist' \
  --exclude='build' \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='.envrc' \
  --exclude='.direnv' \
  --exclude='secrets' \
  --exclude='*.pem' \
  --exclude='*.key' \
  --exclude='id_rsa*' \
  --exclude='*.kdbx' \
  --exclude='.idea' \
  --exclude='.vscode' \
  --exclude='.DS_Store' \
  -C "$REPO_ROOT" -cf - . | tar -C "$BERTH_SRC" -xf -
chown -R berth:berth "$BERTH_SRC"

echo "==> Installing berth into ${BERTH_VENV} with hash-pinned deps"
# Always recreate the venv. This wipes ~* partial-install markers that older
# runs leave behind in
# site-packages, which otherwise spam pip warnings on every subsequent install.
rm -rf "$BERTH_VENV"
run_as_berth python3 -m venv "$BERTH_VENV"
# Bootstrap pip + uv inside the venv. uv reads uv.lock and emits a pip-style
# requirements file with hashes for every transitive dep, which we then install
# with --require-hashes so the VPS sees exactly what CI tested.
run_as_berth "${BERTH_VENV}/bin/python" -m pip install --upgrade --no-cache-dir pip wheel uv
REQ_LOCK="$(mktemp /tmp/berth-requirements.XXXXXX.txt)"
chmod 0644 "$REQ_LOCK"
trap 'rm -f "$REQ_LOCK"' EXIT
# Run uv from inside $BERTH_SRC: uv walks up from CWD looking for uv.toml, and
# the berth user can't traverse e.g. /root/berth/ if the operator launched the
# script from there. ``--no-config`` belts-and-braces against any host-wide
# uv config that would otherwise be searched.
(
  cd "$BERTH_SRC"
  run_as_berth "${BERTH_VENV}/bin/uv" export \
    --frozen --no-dev --no-emit-project --no-config \
    --format requirements-txt > "$REQ_LOCK"
  run_as_berth "${BERTH_VENV}/bin/uv" pip install \
    --python "${BERTH_VENV}/bin/python" \
    --no-config \
    --require-hashes \
    --requirement "$REQ_LOCK"
  # Install the project itself separately, with --no-deps so we don't pull in
  # anything not already in the hash-locked set.
  run_as_berth "${BERTH_VENV}/bin/uv" pip install \
    --python "${BERTH_VENV}/bin/python" \
    --no-config \
    --no-deps \
    "$BERTH_SRC"
)

echo "==> Installing /usr/local/bin/berth operator wrapper"
install -d -m 0755 /etc/berth
cat >/etc/berth/operator.env <<EOF
BERTH_REAL=${BERTH_VENV}/bin/berth
BERTH_HOME=${BERTH_HOME}
BERTH_USER=berth
BERTH_LEADER_URL_DEFAULT=https://${CLUSTER_HOST}
EOF
chmod 0644 /etc/berth/operator.env
install -m 0755 "${BERTH_SRC}/packaging/berth-wrapper" /usr/local/bin/berth

echo "==> Bootstrapping leader config, DB, CA, key pepper, and first admin key"
BOOTSTRAP_ARGS=(
  deploy bootstrap
  --domain "$PUBLIC_HOST"
  --cluster-domain "$CLUSTER_HOST"
  --sni-443
  --leader-only
  --public-port "$PUBLIC_PORT"
  --cluster-port "$CLUSTER_PORT"
  --public-tls-port "$CADDY_TLS_PORT"
  --berth-home "$BERTH_HOME"
)
if [[ "$FORCE" == "1" ]]; then
  BOOTSTRAP_ARGS+=(--force)
fi
run_as_berth env \
  BERTH_HOME="$BERTH_HOME" \
  BERTH_LEADER_URL="https://${CLUSTER_HOST}" \
  "${BERTH_VENV}/bin/berth" "${BOOTSTRAP_ARGS[@]}"

echo "==> Writing berth systemd unit"
cat >/etc/systemd/system/berth.service <<EOF
[Unit]
Description=berth leader daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=berth
Group=berth
WorkingDirectory=${BERTH_HOME}
Environment=BERTH_HOME=${BERTH_HOME}
Environment=BERTH_LEADER_URL=https://${CLUSTER_HOST}
ExecStart=${BERTH_VENV}/bin/berth daemon start --foreground
Restart=on-failure
RestartSec=5

StandardOutput=journal
StandardError=journal

# Process hardening: the leader listens on two high ports and talks to Docker
# over /var/run/docker.sock; it never needs kernel modules, devices, raw
# sockets, or capabilities of any kind.
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
ProcSubset=pid
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
RemoveIPC=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources
CapabilityBoundingSet=
AmbientCapabilities=
UMask=0077
ReadWritePaths=${BERTH_HOME}

[Install]
WantedBy=multi-user.target
EOF

echo "==> Writing Caddy config"
cat >/etc/caddy/Caddyfile <<EOF
{
    auto_https disable_redirects
}

http://${PUBLIC_HOST} {
    redir https://${PUBLIC_HOST}{uri} permanent
}

http://${CLUSTER_HOST} {
    respond 404
}

https://${PUBLIC_HOST}:${CADDY_TLS_PORT} {
    bind 127.0.0.1
    header {
        # Caddy is behind HAProxy on :443; do not advertise loopback
        # :${CADDY_TLS_PORT} as an external HTTP/3 endpoint.
        -Alt-Svc
        Strict-Transport-Security "max-age=31536000"
    }
    reverse_proxy 127.0.0.1:${PUBLIC_PORT} {
        header_up X-Forwarded-Proto https
    }
}

# Catch-all on the same loopback listener: any request whose Host header does
# not match the public site above gets 421 Misdirected Request instead of
# Caddy's empty 200. tls internal uses Caddy's local CA, which is fine here —
# this site only sees connections that HAProxy already routed via the leader
# SNI but with a wrong Host header.
https://:${CADDY_TLS_PORT} {
    bind 127.0.0.1
    tls internal
    respond 421
}
EOF

echo "==> Writing HAProxy config"
cat >/etc/haproxy/haproxy.cfg <<EOF
global
    log /dev/log local0
    log /dev/log local1 notice
    chroot /var/lib/haproxy
    stats socket /run/haproxy/admin.sock mode 660 level admin
    stats timeout 30s
    user haproxy
    group haproxy
    daemon

defaults
    log global
    mode tcp
    option tcplog
    timeout connect 5s
    timeout client  1h
    timeout server  1h

frontend berth_https
    bind *:443
    tcp-request inspect-delay 5s
    tcp-request content accept if { req.ssl_hello_type 1 }
    # Drop connections that don't carry one of our two expected SNI values.
    # No default_backend: anything not matched here has already been rejected.
    tcp-request content reject if !{ req.ssl_sni -i ${CLUSTER_HOST} ${PUBLIC_HOST} }
    use_backend berth_cluster if { req.ssl_sni -i ${CLUSTER_HOST} }
    use_backend berth_public if { req.ssl_sni -i ${PUBLIC_HOST} }

backend berth_public
    server caddy_public 127.0.0.1:${CADDY_TLS_PORT} check

backend berth_cluster
    server berth_cluster 127.0.0.1:${CLUSTER_PORT} check
EOF

echo "==> Validating service configs"
caddy validate --config /etc/caddy/Caddyfile
haproxy -c -f /etc/haproxy/haproxy.cfg

echo "==> Configuring firewall"
ufw allow OpenSSH || ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> Enabling unattended security upgrades"
cat >/etc/apt/apt.conf.d/52berth-unattended <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::Automatic-Reboot "false";
EOF
systemctl enable --now unattended-upgrades

echo "==> Kernel hardening (sysctl)"
cat >/etc/sysctl.d/99-berth.conf <<'EOF'
# This host is not a router.
net.ipv4.ip_forward = 0
net.ipv6.conf.all.forwarding = 0
# Reverse-path filtering (drop packets that arrive on the wrong interface).
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
# Reject ICMP redirects — we don't update routes from hostile networks.
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
# No source-routed packets.
net.ipv4.conf.all.accept_source_route = 0
net.ipv6.conf.all.accept_source_route = 0
# Log spoofed/redirect/source-routed packets.
net.ipv4.conf.all.log_martians = 1
# SYN flood mitigation.
net.ipv4.tcp_syncookies = 1
# Restrict kernel pointer / dmesg disclosure.
kernel.dmesg_restrict = 1
kernel.kptr_restrict = 2
# ptrace only by descendants of the tracer (admin can override).
kernel.yama.ptrace_scope = 2
EOF
sysctl --system >/dev/null

echo "==> SSH hardening"
install -d -m 0755 /etc/ssh/sshd_config.d
cat >/etc/ssh/sshd_config.d/99-berth.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
LoginGraceTime 30
EOF
# Validate before reloading: a broken sshd_config + reload could lock us out.
if sshd -t; then
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
else
  echo "warn: sshd config validation failed; not reloading. Edit /etc/ssh/sshd_config.d/99-berth.conf." >&2
fi

echo "==> Enabling fail2ban for SSH"
install -d -m 0755 /etc/fail2ban/jail.d
cat >/etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
backend = systemd
bantime = 1h
findtime = 10m
maxretry = 5
EOF
systemctl enable --now fail2ban

echo "==> Starting services"
systemctl daemon-reload
systemctl enable --now berth
systemctl enable caddy
systemctl restart caddy
systemctl enable --now haproxy
systemctl restart haproxy

echo ""
echo "==> Done"
echo "Public UI/API:   https://${PUBLIC_HOST}"
echo "Agent endpoint:  https://${CLUSTER_HOST}"
echo ""
echo "Verify:"
echo "  curl https://${PUBLIC_HOST}/healthz"
echo "  curl -k https://${CLUSTER_HOST}/admin/ca.pem"
echo "  berth status"
echo ""
echo "Enroll an agent from the leader:"
echo "  berth nodes enroll <label>"
echo ""
echo "Reset local state if you need to start over:"
echo "  berth wipe"
echo ""
echo "Check logs:"
echo "  journalctl -u berth -n 100 --no-pager"
echo "  journalctl -u caddy -n 100 --no-pager"
echo "  journalctl -u haproxy -n 100 --no-pager"
