#!/usr/bin/env python3
"""Prove that the pinned scanner detects a synthetic token without exposing it."""
import hashlib
import subprocess
import sys

scanner = sys.argv[1]


def scan(text: str) -> int:
    result = subprocess.run(
        [scanner, "stdin", "--no-banner", "--redact"],
        input=text, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=30, check=False,
    )
    return result.returncode


safe = "This fixture contains no credential material.\n"
synthetic = "ghp_" + hashlib.sha256(b"bootstrap scanner canary").hexdigest()[:36]
if scan(safe) != 0:
    raise SystemExit("Pinned secret scanner rejected its safe control fixture.")
if scan("fixture_token=" + synthetic + "\n") == 0:
    raise SystemExit("Pinned secret scanner missed its synthetic GitHub-token canary.")
print("PASS: pinned secret scanner positive and negative controls (value suppressed)")
