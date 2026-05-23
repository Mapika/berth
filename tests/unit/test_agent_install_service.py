from berth.cli.agent_cmd import _render_agent_unit


def test_user_unit_has_execstart_and_home():
    u = _render_agent_unit(berth_bin="/opt/b/bin/berth",
                           berth_home="/home/x/.berth", system=False, run_user=None)
    assert "ExecStart=/opt/b/bin/berth agent start" in u
    assert "Environment=BERTH_HOME=/home/x/.berth" in u
    assert "Restart=on-failure" in u
    assert "User=" not in u


def test_system_unit_sets_user():
    u = _render_agent_unit(berth_bin="/opt/b/bin/berth",
                           berth_home="/home/x/.berth", system=True, run_user="x")
    assert "User=x" in u
    assert "ExecStart=/opt/b/bin/berth agent start" in u
