#!/usr/bin/env python3
"""Assert the WSL boundary settings in the freshly bootstrapped CI container."""
import configparser

config = configparser.ConfigParser(interpolation=None)
config.optionxform = str
with open('/etc/wsl.conf', encoding='utf-8') as source:
    config.read_file(source)
for section, key, expected in (
    ('boot', 'systemd', 'true'),
    ('user', 'default', 'agent'),
    ('interop', 'appendWindowsPath', 'false'),
    ('automount', 'enabled', 'false'),
    ('interop', 'enabled', 'false'),
    ('network', 'generateHosts', 'false'),
):
    actual = config.get(section, key, fallback=None)
    if actual != expected:
        raise SystemExit(f'FAIL: [{section}] {key}: expected {expected}, got {actual}')
print('PASS: all six WSL configuration keys match.')
# system/base.sh blocks Desktop Commander's vendor hosts, exactly once.
with open('/etc/hosts', encoding='utf-8') as source:
    hosts = source.read()
for host in ('desktopcommander.app', 'telemetry.desktopcommander.app', 'mcp.desktopcommander.app',
             'dc-telemetry-proxy-83847352264.europe-west1.run.app'):
    if hosts.count(f'0.0.0.0 {host}\n') != 1 or hosts.count(f':: {host}\n') != 1:
        raise SystemExit(f'FAIL: /etc/hosts does not block {host} exactly once.')
print('PASS: /etc/hosts blocks the Desktop Commander vendor hosts.')
