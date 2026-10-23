#!/usr/bin/python3
"""Checkout gate entry using this checkout's module and policy, never installed."""
import importlib.util
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('checkout_caps', root / 'system/agent-policy/package_caps.py')
caps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caps)
sys.exit(caps.main(policy_path=root / 'system/agent-policy/package-caps.toml'))
