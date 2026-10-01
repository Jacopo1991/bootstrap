#!/usr/bin/env python3
"""CI-only checks for managed helper entries; never calls gh or handles credentials."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HELPER = "!/usr/bin/gh auth git-credential"
source = Path(sys.argv[1]).read_text(encoding="utf-8")


def git(*args, env=None, input=None):
    return subprocess.run(["git", *args], env=env, input=input,
                          capture_output=True, text=True, check=False)


with tempfile.TemporaryDirectory(prefix="bootstrap-git-helper-") as temp:
    root = Path(temp)
    config = root / "gitconfig"
    config.write_text(source, encoding="utf-8")
    for host in ("github.com", "gist.github.com"):
        result = git("config", "--file", str(config), "--get-all",
                     f"credential.https://{host}.helper")
        assert result.returncode == 0
        assert result.stdout.splitlines() == ["", HELPER], host

    # Use inert spies to exercise Git's real helper-reset semantics.
    # They record calls only; neither spy returns any authentication material.
    inherited = root / "inherited.sh"
    helper = root / "gh-spy.sh"
    inherited.write_text("#!/bin/sh\nprintf inherited >> \"$TEST_HELPER_LOG\"\n",
                         encoding="utf-8")
    helper.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$TEST_HELPER_LOG\"\ncat >/dev/null\n",
                      encoding="utf-8")
    inherited.chmod(0o700)
    helper.chmod(0o700)
    system = root / "system"
    system.write_text(f"[credential]\nhelper = !{inherited}\n", encoding="utf-8")
    spy_config = root / "spy-config"
    spy_source = source.replace("!/usr/bin/gh auth git-credential",
                                f"!{helper} auth git-credential")
    spy_config.write_text(spy_source, encoding="utf-8")
    log = root / "calls"
    environment = {
        "PATH": os.defpath, "HOME": str(root), "XDG_CONFIG_HOME": str(root / "xdg"),
        "GIT_CONFIG_SYSTEM": str(system), "GIT_CONFIG_GLOBAL": str(spy_config),
        "GIT_TERMINAL_PROMPT": "0", "TEST_HELPER_LOG": str(log),
    }
    for host in ("github.com", "gist.github.com"):
        log.unlink(missing_ok=True)
        result = git("-C", str(root), "credential", "fill", env=environment,
                     input=f"protocol=https\nhost={host}\n\n")
        assert result.returncode != 0, "inert helper cannot authenticate"
        assert log.read_text(encoding="utf-8").splitlines() == [
            "auth git-credential get"
        ], f"{host}: inherited helper must be cleared and gh helper selected"

    # Prove the behavioral check detects a missing reset on either host.
    for host in ("github.com", "gist.github.com"):
        broken = spy_source.replace(f'[credential "https://{host}"]\n    helper =\n',
                                   f'[credential "https://{host}"]\n', 1)
        assert broken != spy_source
        spy_config.write_text(broken, encoding="utf-8")
        log.unlink(missing_ok=True)
        git("-C", str(root), "credential", "fill", env=environment,
            input=f"protocol=https\nhost={host}\n\n")
        assert "inherited" in log.read_text(encoding="utf-8"), host

print("PASS: both managed gh helpers, inherited-helper resets and negative controls")
