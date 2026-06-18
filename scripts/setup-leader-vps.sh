#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  sudo ./scripts/setup-leader-vps.sh <base-domain> [--force] [--verbose]

Example:
  sudo ./scripts/setup-leader-vps.sh berth.run

Creates a leader-only VPS deployment:
  leader.<base-domain>  public UI/API on external 443
  cluster.<base-domain> agent enrollment + mTLS WebSocket on external 443

Options:
  --force      Overwrite an existing config.toml.
  --verbose    Stream all package/build output live instead of logging it
               quietly to the install log.

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
VERBOSE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=1
      shift
      ;;
    -v|--verbose)
      VERBOSE=1
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

# ── output harness ──────────────────────────────────────────────────────────
# Each provisioning step runs as a function whose full output is captured to
# $LOG. By default only a one-line status is shown; --verbose streams live.
# A step's commands are run in a `set -eo pipefail` subshell so the first
# failing command aborts the step and we surface it (tail of log + path).
LOG="/var/log/berth-install.log"
TOTAL_STEPS=12
_step_n=0

if [[ -t 1 ]]; then
  _c_dim=$'\e[2m'; _c_ok=$'\e[32m'; _c_err=$'\e[31m'
  _c_accent=$'\e[38;5;208m'; _c_off=$'\e[0m'
else
  _c_dim=''; _c_ok=''; _c_err=''; _c_accent=''; _c_off=''
fi

BOOTSTRAP_OUT="$(mktemp /tmp/berth-bootstrap.XXXXXX.out)"
chmod 0600 "$BOOTSTRAP_OUT"
# uv emits a hash-pinned requirements file here; created in the parent so the
# EXIT trap can clean it up regardless of which step fails.
REQ_LOCK="$(mktemp /tmp/berth-requirements.XXXXXX.txt)"
chmod 0644 "$REQ_LOCK"
ADMIN_KEY=""
cleanup() { rm -f "$BOOTSTRAP_OUT" "$REQ_LOCK"; }
trap cleanup EXIT

_rule() {
  printf '  %s────────────────────────────────────────────────────────────%s\n' \
    "$_c_dim" "$_c_off"
}

_label() {
  # "[ n/N] label ········· " with dot leaders, no trailing newline.
  local label="$1" n
  n=$(( 48 - ${#label} ))
  (( n < 0 )) && n=0
  local dots
  dots=$(printf '%*s' "$n" '')
  dots=${dots// /·}
  printf '  %s[%2d/%d]%s %s %s%s%s ' \
    "$_c_dim" "$_step_n" "$TOTAL_STEPS" "$_c_off" "$label" "$_c_dim" "$dots" "$_c_off"
}

_fail() {
  printf '%sFAILED%s\n\n' "$_c_err" "$_c_off"
  printf '  %slast lines of %s:%s\n' "$_c_dim" "$LOG" "$_c_off"
  tail -n 20 "$LOG" 2>/dev/null | sed 's/^/    /'
  printf '\n  full log: %s\n' "$LOG"
  exit 1
}

_redact_secrets() {
  # Mask minted API keys (sk-...) so they never persist in $LOG.
  sed 's/sk-[A-Za-z0-9_-]\{8,\}/sk-***REDACTED***/g'
}

step() {
  local label="$1"; shift
  _step_n=$(( _step_n + 1 ))
  _label "$label"
  local started=$SECONDS rc=0
  # Run the step body in a `set -eo pipefail` subshell so the first failing
  # command aborts it. The parent's -e is toggled off around the call (not an
  # `if` condition) — otherwise bash ignores -e *inside* the subshell too, and
  # a mid-step failure would slip through. `step` is always called as a plain
  # statement, which keeps -e honoured all the way down.
  set +e
  # Defense in depth: redact any sk-... secret before it reaches $LOG so the
  # admin key never lands on disk even if the file's perms are loosened later.
  # (The operator-facing one-time display reads $BOOTSTRAP_OUT, which is 0600
  # and removed on EXIT, so it is unaffected.)
  if [[ $VERBOSE == 1 ]]; then
    printf '\n'
    ( set -eo pipefail; "$@" ) 2>&1 \
      | tee >(_redact_secrets >>"$LOG")
    rc=${PIPESTATUS[0]}
  else
    ( set -eo pipefail; "$@" ) 2>&1 | _redact_secrets >>"$LOG"
    rc=${PIPESTATUS[0]}
  fi
  set -e
  (( rc == 0 )) || _fail
  local elapsed=$(( SECONDS - started ))
  if (( elapsed > 2 )); then
    printf '%sok%s  %ss\n' "$_c_ok" "$_c_off" "$elapsed"
  else
    printf '%sok%s\n' "$_c_ok" "$_c_off"
  fi
}

# ── steps (commands preserved verbatim from the original installer) ──────────

do_packages() {
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
}

do_user() {
  if ! id berth >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$BERTH_HOME" --shell /usr/sbin/nologin berth
  fi
  install -d -o berth -g berth -m 0700 "$BERTH_HOME"
  install -d -o berth -g berth -m 0755 "$BERTH_OPT"
}

do_copy_src() {
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
}

do_install_berth() {
  # Always recreate the venv. This wipes ~* partial-install markers that older
  # runs leave behind in
  # site-packages, which otherwise spam pip warnings on every subsequent install.
  rm -rf "$BERTH_VENV"
  run_as_berth python3 -m venv "$BERTH_VENV"
  # Bootstrap pip + uv inside the venv. uv reads uv.lock and emits a pip-style
  # requirements file with hashes for every transitive dep, which we then install
  # with --require-hashes so the VPS sees exactly what CI tested.
  run_as_berth "${BERTH_VENV}/bin/python" -m pip install --upgrade --no-cache-dir pip wheel uv
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
}

do_wrapper() {
  install -d -m 0755 /etc/berth
  cat >/etc/berth/operator.env <<EOF
BERTH_REAL=${BERTH_VENV}/bin/berth
BERTH_HOME=${BERTH_HOME}
BERTH_USER=berth
BERTH_LEADER_URL_DEFAULT=https://${CLUSTER_HOST}
EOF
  chmod 0644 /etc/berth/operator.env
  install -m 0755 "${BERTH_SRC}/packaging/berth-wrapper" /usr/local/bin/berth
}

# BOOTSTRAP_ARGS built here; --quiet keeps the daemon's output to the status
# block + admin key (this script performs the manual "next steps" itself).
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
  --quiet
)
if [[ "$FORCE" == "1" ]]; then
  BOOTSTRAP_ARGS+=(--force)
fi

do_bootstrap() {
  run_as_berth env \
    BERTH_HOME="$BERTH_HOME" \
    BERTH_LEADER_URL="https://${CLUSTER_HOST}" \
    "${BERTH_VENV}/bin/berth" "${BOOTSTRAP_ARGS[@]}" | tee "$BOOTSTRAP_OUT"
}

do_systemd() {
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
}

do_proxy_config() {
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
}

do_validate() {
  caddy validate --config /etc/caddy/Caddyfile
  haproxy -c -f /etc/haproxy/haproxy.cfg
}

do_firewall_upgrades() {
  ufw allow OpenSSH || ufw allow 22/tcp
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable

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
}

do_hardening() {
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
}

do_start_services() {
  systemctl daemon-reload
  systemctl enable --now berth
  systemctl enable caddy
  systemctl restart caddy
  systemctl enable --now haproxy
  systemctl restart haproxy
}

print_header() {
  # Create the log private (0600). The bootstrap step's stdout carries the
  # freshly minted sk-... admin key; a world-readable log would leak it.
  install -m 0600 /dev/null "$LOG"
  printf '\n  %sberth%s leader installer    %s%s%s\n' \
    "$_c_accent" "$_c_off" "$_c_dim" "$BASE_DOMAIN" "$_c_off"
  _rule
}

print_summary() {
  printf '\n'
  _rule
  printf '  %s✓%s berth leader is up\n\n' "$_c_ok" "$_c_off"
  if [[ -n "$ADMIN_KEY" ]]; then
    printf '    Admin key  %s(shown once — save it now)%s\n' "$_c_dim" "$_c_off"
    printf '        %s%s%s\n\n' "$_c_accent" "$ADMIN_KEY" "$_c_off"
  else
    printf '    Admin key  %s(existing key retained — not re-minted)%s\n\n' \
      "$_c_dim" "$_c_off"
  fi
  printf '    Public UI/API     https://%s\n' "$PUBLIC_HOST"
  printf '    Agent endpoint    https://%s\n\n' "$CLUSTER_HOST"
  printf '    Verify            berth status\n'
  printf '    Enroll an agent   berth nodes enroll <label>\n'
  printf '    Reset state       berth wipe\n'
  printf '    Install log       %s\n' "$LOG"
  _rule
  printf '\n'
}

# ── run ──────────────────────────────────────────────────────────────────────
print_header
step "Installing OS packages"               do_packages
step "Creating berth user and directories"  do_user
step "Copying checkout to ${BERTH_SRC}"      do_copy_src
step "Installing berth (hash-pinned deps)"   do_install_berth
step "Installing operator wrapper"           do_wrapper
step "Bootstrapping config, CA, DB, key"     do_bootstrap
ADMIN_KEY="$(grep -oE 'sk-[A-Za-z0-9_-]+' "$BOOTSTRAP_OUT" 2>/dev/null | head -n1 || true)"
step "Writing systemd unit"                  do_systemd
step "Writing Caddy + HAProxy config"        do_proxy_config
step "Validating service configs"            do_validate
step "Firewall + unattended upgrades"        do_firewall_upgrades
step "Kernel + SSH hardening + fail2ban"     do_hardening
step "Starting services"                     do_start_services
print_summary
