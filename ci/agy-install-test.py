#!/usr/bin/env python3
"""Exercise agy restore behavior with a fake release tarball and binary."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
wrapper = ROOT / "home/dot_local/bin/executable_agy"
assert wrapper.is_file(), "agy wrapper is missing"
wrapper_text = wrapper.read_text(encoding="utf-8")
assert 'AGY_CLI_DISABLE_AUTO_UPDATE=true' in wrapper_text
assert 'exec "$HOME/.local/share/bootstrap/agy/1.4.7/antigravity" "$@"' in wrapper_text
install_source = (ROOT / "home/.chezmoitemplates/agy-install.sh").read_text(encoding="utf-8")
assert "AGY_CLI_DISABLE_AUTO_UPDATE=true" in install_source

with tempfile.TemporaryDirectory() as temp:
    home = Path(temp) / "home"
    home.mkdir()
    tar_root = Path(temp) / "tar-root"
    tar_root.mkdir()
    fake_binary = tar_root / "antigravity"
    fake_binary.write_text("#!/bin/sh\nprintf '%s\\n' 'agy 1.4.7'\n", encoding="utf-8")
    fake_binary.chmod(0o755)
    archive = Path(temp) / "agy.tar.gz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(tar_root), "antigravity"], check=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    version_dir = home / ".local/share/bootstrap/agy/1.4.7"
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
        f"HOME={str(home)!r}\nAGY_URL='fake://agy/1.4.7/archive'\n"
        f"AGY_VERSION='1.4.7'\nAGY_SHA256='{digest}'\ntmp={str(Path(temp) / 'tmp')!r}\nmkdir -p \"$tmp\"\n"
        'FAKE_AGY_ARCHIVE="$FAKE_AGY_ARCHIVE"\ninstall_agy\n',
        encoding="utf-8",
    )
    env = os.environ | {"FAKE_AGY_ARCHIVE": str(archive)}
    subprocess.run(["bash", str(harness)], check=True, env=env)
    assert installed.read_bytes() == fake_binary.read_bytes(), "pinned binary was not restored"
    assert (version_dir.stat().st_mode & 0o222) == 0, "version directory must be read-only"
    assert (installed.stat().st_mode & 0o222) == 0, "pinned binary must be read-only"
    assert not (bin_dir / "agy").is_symlink(), "agy entry point must be a wrapper"
    assert (bin_dir / "agy").read_text(encoding="utf-8") == wrapper_text, "installed wrapper differs from static wrapper"
    installed.chmod(0o755)
    installed.write_text("#!/bin/sh\nprintf '%s\\n' 'agy 1.4.7'\nprintf tampered\n", encoding="utf-8")
    subprocess.run(["bash", str(harness)], check=True, env=env)
    assert installed.read_bytes() == fake_binary.read_bytes(), "same-version SHA mismatch was not restored"
    assert (version_dir.stat().st_mode & 0o222) == 0, "version directory lost read-only mode"
print("PASS: version and same-version SHA mismatches restore the fake pinned binary and keep its version directory read-only")
