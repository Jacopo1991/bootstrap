#!/usr/bin/env python3
"""Exercise agy restore behavior with a fake release tarball and binary."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
pins_text = (ROOT / "home/.chezmoitemplates/pins.env").read_text(encoding="utf-8")
pins = dict(re.findall(r"^([A-Z0-9_]+)='([^']*)'$", pins_text, re.M))
version = pins["AGY_VERSION"]
install_source = (ROOT / "home/.chezmoitemplates/agy-install.sh").read_text(encoding="utf-8")
assert "AGY_CLI_DISABLE_AUTO_UPDATE=true" in install_source

# Render the same source used by ci/lint.sh and inspect its complete install script.
chezmoi = shutil.which("chezmoi")
shellcheck = shutil.which("shellcheck")
assert chezmoi and shellcheck, "chezmoi and shellcheck are required for rendered install coverage"
with tempfile.TemporaryDirectory() as temp:
    destination = Path(temp) / "home"
    config = Path(temp) / "chezmoi.toml"
    subprocess.run([chezmoi, "--source", str(ROOT), "--destination", str(destination),
                    "--config", str(config), "init"], check=True,
                   env=os.environ | {"CHEZMOI_GIT_NAME": "Bootstrap CI",
                                     "CHEZMOI_GIT_EMAIL": "bootstrap-ci@example.invalid"},
                   capture_output=True, text=True)
    rendered = subprocess.run([chezmoi, "--source", str(ROOT), "--destination", str(destination),
                               "--config", str(config), "execute-template"],
                              input=(ROOT / "home/run_onchange_after_10-tools.sh.tmpl").read_text(),
                              check=True, capture_output=True, text=True).stdout
    assert "{{" not in rendered, "unrendered template expression remains"
    assert rendered.index("install_agy() {") < rendered.index("\ninstall_agy\n"), \
        "rendered script must define install_agy before calling it"
    assert not re.search(r"shellcheck disable=[^\n]*(?:SC1009|SC1054|SC1056|SC1072|SC1073|SC1083)",
                          rendered), "rendered script must not hide template parse diagnostics"
    rendered_path = Path(temp) / "tools.sh"
    rendered_path.write_text(rendered, encoding="utf-8")
    checked = subprocess.run([shellcheck, "--exclude=SC2034", str(rendered_path)],
                             capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr

with tempfile.TemporaryDirectory() as temp:
    home = Path(temp) / "home"
    home.mkdir()
    tar_root = Path(temp) / "tar-root"
    tar_root.mkdir()
    fake_binary = tar_root / "antigravity"
    fake_binary.write_text(
        "#!/bin/sh\nprintf 'agy %s auto=%s\\n' '" + version + "' \"${AGY_CLI_DISABLE_AUTO_UPDATE:-unset}\"\n",
        encoding="utf-8",
    )
    fake_binary.chmod(0o755)
    archive = Path(temp) / "agy.tar.gz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(tar_root), "antigravity"], check=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    version_dir = home / ".local/share/bootstrap/agy" / version
    version_dir.mkdir(parents=True)
    installed = version_dir / "antigravity"
    installed.write_text("#!/bin/sh\nprintf '%s\\n' 'agy 1.4.8'\n", encoding="utf-8")
    installed.chmod(0o755)
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    download_stub = Path(temp) / "download-stub.sh"
    download_stub.write_text(
        "download() { cp \"$FAKE_AGY_ARCHIVE\" \"$3\"; "
        "printf '%s  %s\\n' \"$2\" \"$3\" | sha256sum --check --status; }\n",
        encoding="utf-8",
    )
    harness = Path(temp) / "run-install.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"source {str(download_stub)!r}\n"
        f"source {str(ROOT / 'home/.chezmoitemplates/agy-install.sh')!r}\n"
        f"HOME={str(home)!r}\nAGY_URL='fake://agy/{version}/archive'\n"
        f"AGY_VERSION='{version}'\nAGY_SHA256='{digest}'\ntmp={str(Path(temp) / 'tmp')!r}\nmkdir -p \"$tmp\"\n"
        'FAKE_AGY_ARCHIVE="$FAKE_AGY_ARCHIVE"\ninstall_agy\n',
        encoding="utf-8",
    )
    env = os.environ | {"FAKE_AGY_ARCHIVE": str(archive)}
    subprocess.run(["bash", str(harness)], check=True, env=env)
    assert installed.read_bytes() == fake_binary.read_bytes(), "pinned binary was not restored"
    assert (version_dir.stat().st_mode & 0o222) == 0, "version directory must be read-only"
    assert (installed.stat().st_mode & 0o222) == 0, "pinned binary must be read-only"
    assert not (bin_dir / "agy").is_symlink(), "agy entry point must be a wrapper"
    wrapper_text = (bin_dir / "agy").read_text(encoding="utf-8")
    assert "AGY_CLI_DISABLE_AUTO_UPDATE=true" in wrapper_text
    assert f'exec "$HOME/.local/share/bootstrap/agy/{version}/antigravity" "$@"' in wrapper_text
    wrapped = subprocess.run([str(bin_dir / "agy")], env=os.environ | {"HOME": str(home)},
                             check=True, capture_output=True, text=True)
    assert wrapped.stdout.strip() == f"agy {version} auto=true", wrapped.stdout
    installed.chmod(0o755)
    installed.write_text("#!/bin/sh\nprintf tampered\n", encoding="utf-8")
    subprocess.run(["bash", str(harness)], check=True, env=env)
    assert installed.read_bytes() == fake_binary.read_bytes(), "same-version SHA mismatch was not restored"
    assert (version_dir.stat().st_mode & 0o222) == 0, "version directory lost read-only mode"
print("PASS: version and same-version SHA mismatches restore the fake pinned binary and keep its version directory read-only")
