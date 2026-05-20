from __future__ import annotations

import typer

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="single-node inference service router",
)

from berth.cli import (  # noqa: E402
    adapter_cmd,  # noqa: F401  registers `adapter` sub-app
    agent_cmd,  # noqa: F401  registers `agent` sub-app
    backup_cmd,  # noqa: F401  registers `backup` sub-app
    config_cmd,  # noqa: F401  registers `config` sub-app
    daemon_cmd,  # noqa: F401  registers `daemon` sub-app
    deploy_cmd,  # noqa: F401  registers `deploy` sub-app
    doctor_cmd,  # noqa: F401  registers command
    key_cmd,  # noqa: F401  registers `key` sub-app
    logs_cmd,  # noqa: F401  registers command
    ls_cmd,  # noqa: F401  registers command
    nodes_cmd,  # noqa: F401  registers `nodes` sub-app
    pin_cmd,  # noqa: F401  registers `pin` and `unpin` commands
    predict_cmd,  # noqa: F401  registers `predict` command
    ps_cmd,  # noqa: F401  registers command
    pull_cmd,  # noqa: F401  registers command
    run_cmd,  # noqa: F401  registers command
    setup_cmd,  # noqa: F401  registers command
    status_cmd,  # noqa: F401  registers command
    stop_cmd,  # noqa: F401  registers command
    top_cmd,  # noqa: F401  registers command
    update_engines_cmd,  # noqa: F401  registers command
    wipe_cmd,  # noqa: F401  registers command
)
