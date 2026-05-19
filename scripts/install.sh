#!/usr/bin/env bash
set -euo pipefail

# berth bootstrap installer.
#
# Usage:
#   curl -fsSL https://example.com/install.sh | bash
#
# What it does:
#   1. Installs `uv` if missing (https://docs.astral.sh/uv/).
#   2. `uv tool install` the `berth` package (or `pip install -e .` if run inside a checkout).
#   3. Runs `berth doctor`.
#   4. Prints next steps.

REPO_DIR=""
if [ -f "pyproject.toml" ] && grep -q "berth" pyproject.toml 2>/dev/null; then
    REPO_DIR="$(pwd)"
fi

if ! command -v uv >/dev/null 2>&1; then
    echo ">>> installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck source=/dev/null
    [ -f "$HOME/.local/share/uv/env" ] && . "$HOME/.local/share/uv/env" || true
    export PATH="$HOME/.local/bin:$PATH"
fi
echo ">>> uv $(uv --version)"

if [ -n "$REPO_DIR" ]; then
    echo ">>> installing berth from local checkout: $REPO_DIR"
    uv tool install --editable "$REPO_DIR"
else
    echo ">>> installing berth from PyPI (or remote)"
    uv tool install berth
fi

echo
echo ">>> running berth doctor"
if berth doctor; then
    echo
    echo "✓ environment looks good. Next:"
    echo
    echo "    berth setup        # interactive wizard (recommended)"
    echo "    # or:"
    echo "    berth daemon start"
    echo "    berth pull Qwen/Qwen2.5-0.5B-Instruct --name qwen-0_5b"
    echo "    berth run qwen-0_5b --gpu 0"
else
    echo
    echo "! berth doctor reported issues. Fix them and re-run \`berth doctor\`."
    exit 1
fi
