"""Inert systemd user-manager fixture. Never contacts the real manager."""
import textwrap

RUN = r'''#!/usr/bin/env python3
import json, os, pathlib, shlex, subprocess, sys
home = pathlib.Path.home()
args = sys.argv[1:]
if not all(os.environ.get(key) for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS')):
    sys.exit('No user systemd bus: missing XDG_RUNTIME_DIR or DBUS_SESSION_BUS_ADDRESS')
unit = args[args.index('--unit') + 1]
root = home / 'units'
root.mkdir(exist_ok=True)
env = {}
environment_file = pathlib.Path(next(arg.split('=', 1)[1] for arg in args if arg.startswith('EnvironmentFile=')))
for assignment in shlex.split(environment_file.read_text()):
    key, value = assignment.split('=', 1)
    env[key] = value
for arg in args:
    if arg.startswith('--setenv='):
        key, value = arg[9:].split('=', 1)
        env[key] = value
with (home / 'unit-calls.jsonl').open('a') as stream:
    stream.write(json.dumps(dict(args=args, env=env, environment_file=str(environment_file),
                                environment_file_mode=environment_file.stat().st_mode & 0o777)) + '\n')
condition = next(arg.split('=', 1)[1] for arg in args if arg.startswith('ExecCondition='))
if (home / 'pull-before-service').exists():
    (home / '.local/state/task/hold').write_text('service admission hold')
if subprocess.run(shlex.split(condition), env=env).returncode:
    # skip-style: systemd may report a skipped ExecCondition as Result=success with no main process started
    style = (home / 'skip-style').read_text().strip() if (home / 'skip-style').exists() else 'exec-condition'
    (root / unit).write_text(json.dumps(dict(MainPID='0', ActiveState='inactive', ExecMainStartTimestamp='',
                                             Result='exec-condition' if style == 'exec-condition' else 'success')))
    sys.exit(0)
if os.environ.get('SERVICE_EXIT'):
    sys.exit(int(os.environ['SERVICE_EXIT']))
(root / unit).write_text(json.dumps(dict(MainPID=str(os.getpid()), ActiveState='active', Result='success')))
os.chdir(args[args.index('--working-directory') + 1])
command = args[args.index('--') + 1:]
try: os.execvpe(command[0], command, env)
except OSError as error: sys.exit(str(error))
'''

CTL = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, sys, time
home = pathlib.Path.home()
unit = sys.argv[-1]
try: data = json.loads((home / 'units' / unit).read_text())
except FileNotFoundError: sys.exit(1)
try:
    status = pathlib.Path(f"/proc/{data['MainPID']}/stat").read_text().rsplit(')', 1)[1].split()[0]
    if status == 'Z': raise FileNotFoundError()
except FileNotFoundError:
    data.update(MainPID='0', ActiveState='inactive')
if 'stop' in sys.argv:
    (home / 'stopped').write_text(unit)
    def kill_tree(pid, signum):
        try: children = pathlib.Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
        except OSError: children = []
        for child in children: kill_tree(int(child), signum)
        try: os.kill(pid, signum)
        except ProcessLookupError: pass
    if int(data['MainPID']):
        kill_tree(int(data['MainPID']), signal.SIGTERM)
        time.sleep(.2)
        kill_tree(int(data['MainPID']), signal.SIGKILL)
else:
    for key, value in data.items(): print(f'{key}={value}')
'''


def install_units(home):
    directory = home / 'bin'
    directory.mkdir(exist_ok=True)
    for name, body in (('systemd-run', RUN), ('systemctl', CTL)):
        target = directory / name
        target.write_text(textwrap.dedent(body))
        target.chmod(0o755)
