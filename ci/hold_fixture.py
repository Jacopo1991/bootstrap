"""Install the real fail-closed checker with a test-only /etc source substitution."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def install_checker(home):
    home = Path(home)
    target = home / '.local/bin/task'
    target.parent.mkdir(parents=True, exist_ok=True)
    (home / 'etc-agent-hold.lock').touch(mode=0o600)
    source_dir = ROOT / 'home/dot_local/bin'
    source = (source_dir / 'executable_task').read_text()
    source = source.replace("name.startswith('LC_')", "name.startswith(('LC_', 'TASK_TEST_', 'GATE_BROWSER_FIXTURE_')) or name in {'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM'}")
    source = source.replace('"DBUS_SESSION_BUS_ADDRESS"}', '"DBUS_SESSION_BUS_ADDRESS", "GATE_BROWSER_FIXTURE_FAIL"}')
    assert source.count('Path("/etc/agent-hold")') == 1
    target.write_text(source.replace('Path("/etc/agent-hold")', f'Path({str(home / "etc-agent-hold")!r})'))
    target.chmod(0o755)
    for module in source_dir.glob('task_*.py'):
        link = target.parent / module.name
        if not link.exists(): link.symlink_to(module)
    return target
