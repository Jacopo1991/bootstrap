#!/usr/bin/env python3
"""Offline fixture tests for the monthly pin updater."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import unittest

import monthly_pins as pins


class ResolverTests(unittest.TestCase):
    def test_only_stable_exact_versions_are_accepted(self) -> None:
        for value in ("24.21.0", "0.159.2", "2026.9.17"):
            with self.subTest(value=value):
                self.assertTrue(pins.stable_version(value))
        for value in ("24.21.0-rc.1", "v24.21.0", "latest", "24.21"):
            with self.subTest(value=value):
                self.assertFalse(pins.stable_version(value))

    def test_apt_lock_candidate_set_is_atomic_and_ordered(self) -> None:
        lock = "alpha=1.0\nbeta=2.0\n"
        entries = pins.lock_entries(lock)
        self.assertEqual(pins.render_lock(entries, {"alpha": "1.1", "beta": "2.1"}),
                         "alpha=1.1\nbeta=2.1\n")
        with self.assertRaises(pins.ResolveError):
            pins.render_lock(entries, {"alpha": "1.1"})
        self.assertEqual(lock, "alpha=1.0\nbeta=2.0\n")

    def test_version_and_snapshot_resolvers_are_monotonic(self) -> None:
        self.assertTrue(pins.version_is_newer("24.22.0", "24.21.0"))
        self.assertFalse(pins.version_is_newer("24.20.9", "24.21.0"))
        now = dt.datetime(2026, 10, 1, 1, 30, tzinfo=dt.timezone.utc)
        self.assertEqual(
            pins.snapshot_candidates(now, "20260930T000000Z"),
            ["20261001T000000Z"],
        )
        self.assertEqual(pins.snapshot_candidates(now, "20261001T000000Z"), [])

    def test_apt_sources_and_status_are_isolated(self) -> None:
        ubuntu = "Types: deb\nURIs: snapshot\nSuites: noble\n"
        vendor = "deb https://example.invalid stable main\n"
        self.assertEqual(pins.apt_source_filename(ubuntu), "sources.sources")
        self.assertEqual(pins.apt_source_filename(vendor), "sources.list")
        temp = Path("/tmp/apt-probe")
        options = pins.apt_options(
            temp, temp / "sources.sources", temp / "lists", temp / "archives", temp / "status"
        )
        self.assertIn("Dir::Etc::sourceparts=-", options)
        self.assertIn("Dir::Etc::parts=-", options)
        self.assertIn(f"Dir::State::status={temp / 'status'}", options)
        self.assertFalse(any(option == "Dir::State::status=/var/lib/dpkg/status" for option in options))

    def test_base_snapshot_and_lock_stage_only_as_a_complete_cohort(self) -> None:
        old_pins = "APT_SNAPSHOT='20260930T000000Z'\nOTHER='old'\n"
        lock = "alpha=1.0\nbeta=2.0\n"
        result_lock, result_pins = pins.build_base_cohort(
            old_pins, lock, {"alpha": "1.1", "beta": "2.1"}, "20261001T000000Z"
        )
        self.assertEqual(result_lock, "alpha=1.1\nbeta=2.1\n")
        self.assertIn("APT_SNAPSHOT='20261001T000000Z'", result_pins)
        self.assertEqual(old_pins, "APT_SNAPSHOT='20260930T000000Z'\nOTHER='old'\n")
        with self.assertRaises(pins.ResolveError):
            pins.build_base_cohort(
                old_pins, lock, {"alpha": "1.1"}, "20261001T000000Z"
            )
        self.assertEqual(old_pins, "APT_SNAPSHOT='20260930T000000Z'\nOTHER='old'\n")

    def test_apt_versions_cannot_be_downgraded(self) -> None:
        self.assertEqual(
            pins.render_lock(pins.lock_entries("pkg=1.0\n"), {"pkg": "1.1"}),
            "pkg=1.1\n",
        )
        with self.assertRaises(pins.ResolveError):
            pins.render_lock(pins.lock_entries("pkg=1.1\n"), {"pkg": "1.0"})

    def test_malformed_or_duplicate_pin_is_not_silently_rewritten(self) -> None:
        source = "NODE_VERSION='24.21.0'\nPYTHON_VERSION='3.12.14'\n"
        duplicate = source + "NODE_VERSION='24.21.1'\n"
        with self.assertRaises(pins.ResolveError):
            pins.replace_pin(duplicate, "NODE_VERSION", "24.22.0")
        self.assertEqual(pins.replace_pin(source, "PYTHON_VERSION", "3.12.15"),
                         "NODE_VERSION='24.21.0'\nPYTHON_VERSION='3.12.15'\n")

    def test_npm_lock_requires_v2_and_integrity_for_every_package(self) -> None:
        complete = {
            "lockfileVersion": 2,
            "packages": {
                "": {},
                "node_modules/tool": {
                    "resolved": "https://registry.npmjs.org/tool/-/tool.tgz",
                    "integrity": "sha512-abc"
                }
            }
        }
        self.assertTrue(pins.npm_lock_has_full_integrity(complete))
        missing = json.loads(json.dumps(complete))
        del missing["packages"]["node_modules/tool"]["integrity"]
        self.assertFalse(pins.npm_lock_has_full_integrity(missing))
        wrong_version = json.loads(json.dumps(complete))
        wrong_version["lockfileVersion"] = 3
        self.assertFalse(pins.npm_lock_has_full_integrity(wrong_version))

    def test_gate_selection_requires_workflow_branch_event_head_and_fresh_time(self) -> None:
        dispatched = dt.datetime(2026, 10, 1, 1, 30, tzinfo=dt.timezone.utc)
        expected = {
            "workflow_id": 42,
            "event": "workflow_dispatch",
            "head_branch": "automation/monthly-pin-bump-2026-10",
            "head_sha": "new-sha",
            "created_at": "2026-10-01T01:31:00Z",
            "run_number": 8,
            "id": 100,
            "html_url": "https://example.invalid/run/8",
        }
        stale = dict(expected, head_sha="old-sha", run_number=99, id=104)
        prior_same_head = dict(expected, run_number=100, id=101)
        wrong_event = dict(expected, event="pull_request", run_number=98, id=103)
        wrong_branch = dict(expected, head_branch="main", run_number=97, id=102)
        old = dict(expected, created_at="2026-10-01T01:00:00Z", run_number=96, id=105)
        selected = pins.select_gate_run(
            [stale, prior_same_head, wrong_event, wrong_branch, old, expected],
            42,
            "automation/monthly-pin-bump-2026-10",
            "new-sha",
            dispatched,
            {101, 102, 103, 104, 105},
        )
        self.assertIs(selected, expected)
        self.assertIsNone(pins.select_gate_run(
            [stale, prior_same_head, wrong_event, wrong_branch, old],
            42,
            "automation/monthly-pin-bump-2026-10",
            "new-sha",
            dispatched,
            {101, 102, 103, 104, 105},
        ))


if __name__ == "__main__":
    unittest.main()
