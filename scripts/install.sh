#!/usr/bin/env bash
set -euo pipefail

# berth bootstrap installer.
#
# Usage:
#   curl -fsSL https://example.com/install.sh | bash
#   curl -fsSL https://example.com/install.sh | bash -s -- --verbose
#
# What it does:
#   1. Installs `uv` if missing (https://docs.astral.sh/uv/), pinned to
#      $UV_VERSION below. Operators should review this script before piping it
#      into a shell, and may override the pin via UV_VERSION=x.y.z.
#   2. `uv tool install` the `berth` package (or editable, if run in a checkout).
#   3. Runs `berth doctor`.
#   4. Prints next steps.

VERBOSE=0
case "${1:-}" in
  -v|--verbose) VERBOSE=1 ;;
esac

# Pin the uv installer to a known version rather than tracking latest. The
# astral installer honours UV_INSTALL_VERSION; operators may override this.
UV_VERSION="${UV_VERSION:-0.5.11}"

# ── output harness ──────────────────────────────────────────────────────────
# Unlike the leader installer, steps run in the *current* shell (no subshell):
# installing uv mutates PATH for the install-berth step that follows, so the
# environment must persist. Step bodies therefore end on a command whose status
# reflects success, and check failure modes explicitly.
LOG="${TMPDIR:-/tmp}/berth-install.log"
TOTAL_STEPS=2
_step_n=0

if [ -t 1 ]; then
  _c_dim=$'\e[2m'; _c_ok=$'\e[32m'; _c_err=$'\e[31m'
  _c_accent=$'\e[38;5;208m'; _c_off=$'\e[0m'
else
  _c_dim=''; _c_ok=''; _c_err=''; _c_accent=''; _c_off=''
fi

_rule() {
  printf '  %s────────────────────────────────────────────────────────────%s\n' \
    "$_c_dim" "$_c_off"
}

_label() {
  local label="$1" n
  n=$(( 48 - ${#label} ))
  [ "$n" -lt 0 ] && n=0
  local dots
  dots=$(printf '%*s' "$n" '')
  dots=${dots// /·}
  printf '  %s[%d/%d]%s %s %s%s%s ' \
    "$_c_dim" "$_step_n" "$TOTAL_STEPS" "$_c_off" "$label" "$_c_dim" "$dots" "$_c_off"
}

_fail() {
  printf '%sFAILED%s\n\n' "$_c_err" "$_c_off"
  printf '  %slast lines of %s:%s\n' "$_c_dim" "$LOG" "$_c_off"
  tail -n 20 "$LOG" 2>/dev/null | sed 's/^/    /'
  printf '\n  full log: %s\n' "$LOG"
  exit 1
}

step() {
  local label="$1"; shift
  _step_n=$(( _step_n + 1 ))
  _label "$label"
  local rc=0
  set +e
  if [ "$VERBOSE" = 1 ]; then
    printf '\n'
    "$@" 2>&1 | tee -a "$LOG"
    rc=${PIPESTATUS[0]}
  else
    "$@" >>"$LOG" 2>&1
    rc=$?
  fi
  set -e
  [ "$rc" -eq 0 ] || _fail
  printf '%sok%s\n' "$_c_ok" "$_c_off"
}

REPO_DIR=""
if [ -f "pyproject.toml" ] && grep -q "berth" pyproject.toml 2>/dev/null; then
    REPO_DIR="$(pwd)"
fi

ensure_uv() {
  command -v uv >/dev/null 2>&1 && return 0
  # Pin to $UV_VERSION; the installer reads UV_INSTALL_VERSION from the env.
  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" \
    | env UV_INSTALL_VERSION="$UV_VERSION" sh || return 1
  # shellcheck source=/dev/null
  [ -f "$HOME/.local/share/uv/env" ] && . "$HOME/.local/share/uv/env"
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1   # final status = did uv actually land
}

install_berth() {
  if [ -n "$REPO_DIR" ]; then
    uv tool install --editable "$REPO_DIR"
  else
    uv tool install berth
  fi
}

# ── run ──────────────────────────────────────────────────────────────────────
: > "$LOG"
printf '\n  %sberth%s installer\n' "$_c_accent" "$_c_off"
_rule
step "Installing uv"    ensure_uv
step "Installing berth" install_berth

# berth doctor is the informative payload — always show its output. A non-zero
# exit means "issues found" (not a crash), so handle it with a tailored hint
# rather than the generic step-failure path.
printf '\n  %srunning berth doctor%s\n\n' "$_c_dim" "$_c_off"
if berth doctor; then
  printf '\n'
  _rule
  printf '  %s✓%s environment looks good\n\n' "$_c_ok" "$_c_off"
  printf '    Get started       %sberth setup%s   %s# interactive wizard%s\n' \
    "$_c_accent" "$_c_off" "$_c_dim" "$_c_off"
  printf '    Or manually       berth daemon start\n'
  printf '                      berth pull Qwen/Qwen2.5-0.5B-Instruct --name qwen-0_5b\n'
  printf '                      berth run qwen-0_5b --gpu 0\n'
  printf '    Install log       %s\n' "$LOG"
  _rule
  printf '\n'
else
  printf '\n  %s!%s berth doctor reported issues. Fix them and re-run: berth doctor\n' \
    "$_c_err" "$_c_off"
  exit 1
fi
