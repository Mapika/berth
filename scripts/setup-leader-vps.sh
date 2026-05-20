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

if ! [[ "$BASE_DOMAIN" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ]]; then
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
  git \
  haproxy \
  python3-venv \
  ufw

echo "==> Creating berth user and directories"
if ! id berth >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$BERTH_HOME" --shell /usr/sbin/nologin berth
fi
install -d -o berth -g berth -m 0700 "$BERTH_HOME"
install -d -o berth -g berth -m 0755 "$BERTH_OPT"

echo "==> Copying current checkout to ${BERTH_SRC}"
rm -rf "$BERTH_SRC"
install -d -o berth -g berth -m 0755 "$BERTH_SRC"
tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  -C "$REPO_ROOT" -cf - . | tar -C "$BERTH_SRC" -xf -
chown -R berth:berth "$BERTH_SRC"

echo "==> Installing berth into ${BERTH_VENV}"
if [[ ! -x "${BERTH_VENV}/bin/python" ]]; then
  run_as_berth python3 -m venv "$BERTH_VENV"
fi
run_as_berth "${BERTH_VENV}/bin/python" -m pip install --upgrade pip wheel
run_as_berth "${BERTH_VENV}/bin/pip" install "$BERTH_SRC"

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
  --serve-home "$BERTH_HOME"
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

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
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
    use_backend berth_cluster if { req.ssl_sni -i ${CLUSTER_HOST} }
    use_backend berth_public if { req.ssl_sni -i ${PUBLIC_HOST} }
    default_backend berth_public

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

echo "==> Starting services"
systemctl daemon-reload
systemctl enable --now berth
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
echo ""
echo "Enroll an agent from the leader:"
echo "  sudo -u berth env BERTH_HOME=${BERTH_HOME} ${BERTH_VENV}/bin/berth nodes enroll <label>"
echo ""
echo "Check logs:"
echo "  journalctl -u berth -n 100 --no-pager"
echo "  journalctl -u caddy -n 100 --no-pager"
echo "  journalctl -u haproxy -n 100 --no-pager"
