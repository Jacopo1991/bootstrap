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
):
    actual = config.get(section, key, fallback=None)
    if actual != expected:
        raise SystemExit(f'FAIL: [{section}] {key}: expected {expected}, got {actual}')
print('PASS: all five WSL configuration keys match.')
