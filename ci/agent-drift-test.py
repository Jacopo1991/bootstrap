#!/usr/bin/env python3
"""Hosted tests for read-only installed-set drift detection."""
from pathlib import Path
import importlib.util
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent_drift", ROOT / "checks/agent-drift.py")
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)

expected = {"claude", "codex", "bws", "secretspec", "gitleaks", "ccusage"}
assert drift.compare_sets(expected, expected) == []
with tempfile.TemporaryDirectory() as temp:
    home = Path(temp)
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for name in expected:
        (bin_dir / name).touch()
    assert drift.unexpected_global_binaries(home) == []
    (bin_dir / "foreign-agent-cli").touch()
    assert drift.unexpected_global_binaries(home) == ["foreign-agent-cli"]
print("PASS: foreign global agent install is reported as drift")
