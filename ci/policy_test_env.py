"""Explicit environments for policy fixtures, independent of the running lane."""
import atexit
import os
from pathlib import Path
import tempfile


def isolate_policy_environment(*extra_names: str) -> None:
    # Allocate under the caller's writable temp root before replacing its anchors.
    temporary = tempfile.TemporaryDirectory(prefix="policy-fixture-")
    atexit.register(temporary.cleanup)
    base = Path(temporary.name)
    # Lane TMPDIR can be inside the real checkout. Stop Git discovery there:
    # uninitialised fixture directories must not resolve to the enclosing repo.
    # Actual repositories created below this boundary still resolve normally.
    (base / ".git").write_text("gitdir: missing-fixture-repository\n")
    env = {name: os.environ[name] for name in ("PATH", "HOME", *extra_names)
           if name in os.environ}
    env.update(TMPDIR=str(base), XDG_STATE_HOME=str(base / "state"),
               PYTHONDONTWRITEBYTECODE="1")
    os.environ.clear()
    os.environ.update(env)
    tempfile.tempdir = str(base)
