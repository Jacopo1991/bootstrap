#!/usr/bin/env python3
"""Offline fixture tests for the monthly pin updater."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest import mock
import tempfile

import monthly_pins as pins

FAKE_AGY_VERSION = "9.9.9"


class ResolverTests(unittest.TestCase):
    def test_agy_future_pin_has_no_fixture_version_collisions(self) -> None:
        root = Path(pins.__file__).resolve().parents[1]
        future_version = "1.4." + "8"
        findings = []
        for pattern in ("*.py", "*.sh"):
            for path in (root / "ci").glob(pattern):
                content = path.read_text(encoding="utf-8", errors="replace")
                if future_version in content:
                    findings.append(path.relative_to(root).as_posix())
        self.assertEqual(findings, [], "future agy pin collides with monthly test fixtures")

    def test_agy_release_bump_leaves_no_old_pin_outside_named_prose_and_fixtures(self) -> None:
        root = Path(pins.__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "repo"
            copy.mkdir()
            for name in ("home", "checks", "docs", "ci"):
                shutil.copytree(root / name, copy / name)
            shutil.copy2(root / "README.md", copy / "README.md")
            pins_path = copy / "home/.chezmoitemplates/pins.env"
            source = pins_path.read_text(encoding="utf-8")
            values = pins.parse_pins(source)
            old_version = values["AGY_VERSION"]
            old_sha = values["AGY_SHA256"]
            self.assertNotEqual(FAKE_AGY_VERSION, old_version)
            self.assertTrue(pins.version_is_newer(FAKE_AGY_VERSION, old_version))
            notes: list[str] = []
            artifact = f"fixture official agy {FAKE_AGY_VERSION} archive".encode()
            release = {"assets": [{"name": "agy_cli_linux_x64.tar.gz",
                                   "browser_download_url": f"https://example.invalid/agy-{FAKE_AGY_VERSION}.tar.gz"}]}
            with mock.patch.object(pins, "latest_release", return_value=(FAKE_AGY_VERSION, release)), \
                 mock.patch.object(pins, "request_bytes", return_value=artifact) as request:
                updated, _resolved = pins.update_release_pin(
                    source, values, notes, label="agy", repo="google-antigravity/antigravity-cli",
                    version_key="AGY_VERSION", url_key="AGY_URL", sha_key="AGY_SHA256",
                    asset_name=lambda _version: "agy_cli_linux_x64.tar.gz",
                )
            request.assert_called_once_with(f"https://example.invalid/agy-{FAKE_AGY_VERSION}.tar.gz")
            pins_path.write_text(updated, encoding="utf-8")

            findings = []
            for path in [copy / "README.md"] + [
                    path for directory_path in (copy / "home", copy / "checks", copy / "docs")
                    for path in directory_path.rglob("*") if path.is_file()]:
                content = path.read_text(encoding="utf-8", errors="replace")
                relative = path.relative_to(copy).as_posix()
                for needle in (old_version, old_sha):
                    if needle in content and not (relative == "README.md" and needle == old_version):
                        findings.append((relative, needle))
            for pattern in ("*.py", "*.sh"):
                for path in (copy / "ci").glob(pattern):
                    content = path.read_text(encoding="utf-8", errors="replace")
                    findings.extend((path.relative_to(copy).as_posix(), needle)
                                    for needle in (old_version, old_sha) if needle in content)
            self.assertEqual(findings, [], "stale agy pin remains after simulated monthly bump")

    def test_agy_release_refresh_updates_version_url_and_asset_digest(self) -> None:
        old = (
            "AGY_VERSION='1.4.0'\n"
            "AGY_URL='https://github.com/google-antigravity/antigravity-cli/releases/download/1.4.0/agy_cli_linux_x64.tar.gz'\n"
            "AGY_SHA256='fd96967583c1eed94733c039638fa59b71ddbd823c75ebc8dd0e677463565aad'\n"
        )
        artifact = b"fixture official agy release archive"
        release = {"assets": [{"name": "agy_cli_linux_x64.tar.gz",
                               "browser_download_url": f"https://example.invalid/agy-{FAKE_AGY_VERSION}.tar.gz"}]}
        pin_values = pins.parse_pins(old)
        current = pins.parse_pins((Path(pins.__file__).resolve().parents[1] /
                                   "home/.chezmoitemplates/pins.env").read_text(encoding="utf-8"))
        self.assertNotEqual(FAKE_AGY_VERSION, current["AGY_VERSION"])
        self.assertTrue(pins.version_is_newer(FAKE_AGY_VERSION, current["AGY_VERSION"]))
        notes: list[str] = []
        with mock.patch.object(pins, "latest_release", return_value=(FAKE_AGY_VERSION, release)), \
             mock.patch.object(pins, "request_bytes", return_value=artifact) as request:
            updated, resolved = pins.update_release_pin(
                old, pin_values, notes, label="agy", repo="google-antigravity/antigravity-cli",
                version_key="AGY_VERSION", url_key="AGY_URL", sha_key="AGY_SHA256",
                asset_name=lambda version: "agy_cli_linux_x64.tar.gz",
            )
        request.assert_called_once_with(f"https://example.invalid/agy-{FAKE_AGY_VERSION}.tar.gz")
        self.assertEqual(resolved["AGY_VERSION"], FAKE_AGY_VERSION)
        self.assertIn(f"AGY_VERSION='{FAKE_AGY_VERSION}'", updated)
        self.assertIn(f"AGY_URL='https://example.invalid/agy-{FAKE_AGY_VERSION}.tar.gz'", updated)
        self.assertIn(f"AGY_SHA256='{hashlib.sha256(artifact).hexdigest()}'", updated)
        self.assertIn(f"Updated agy to {FAKE_AGY_VERSION}", notes[0])

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

    def test_rootless_extras_must_match_the_engine_version(self) -> None:
        same = "docker-ce=5:29.9.2\ndocker-ce-cli=5:29.9.2\ndocker-ce-rootless-extras=5:29.9.2\n"
        pins.require_engine_match(same)
        pins.require_engine_match("alpha=1.0\n")
        with self.assertRaises(pins.ResolveError):
            pins.require_engine_match(same.replace("rootless-extras=5:29.9.2", "rootless-extras=5:29.9.1"))

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


    def test_open_pr_search_paginates_past_three_pages_and_fails_at_cap(self) -> None:
        target = {"number": 77, "head": {"ref": "automation/monthly-pin-bump-2026-10",
                  "user": {"login": "jacopo1991"}}}
        pages = {1: [{}] * 100, 2: [{}] * 100, 3: [{}] * 100, 4: [target]}
        requested = []
        def fetch(page: int):
            requested.append(page)
            return pages[page]
        self.assertIs(next(p for page in pins.monthly_pr_pages(fetch) for p in page
                           if p.get("number") == 77), target)
        self.assertEqual(requested, [1, 2, 3, 4])

        def full_page(_page: int):
            return [{}] * 100
        with self.assertRaisesRegex(pins.ResolveError, "safety cap"):
            list(pins.monthly_pr_pages(full_page, page_cap=2))

    def test_failed_gate_never_creates_new_pr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.md"
            report.write_text("Retained unresolved tools.\\n", encoding="utf-8")
            api = mock.Mock(return_value={"object": {"sha": "candidate-sha"}})
            with mock.patch.object(pins, "find_monthly_prs", return_value=[]), \
                 mock.patch.object(pins, "dispatch_gate",
                                   return_value=("https://example.invalid/gate/1", "failure")), \
                 mock.patch.object(pins, "api_request", api):
                with self.assertRaisesRegex(pins.ResolveError, "did not pass"):
                    pins.publish_pr("owner/repo", "automation/monthly-pin-bump-2026-10",
                                    "candidate-sha", str(report))
            self.assertFalse(any(call.args[1] == "pulls" and call.args[2] == "POST"
                                 for call in api.call_args_list))

    def test_green_gate_publishes_only_the_exact_checked_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.md"
            report.write_text("Resolved cohort.\\n", encoding="utf-8")
            def api(_repo: str, path: str, method: str = "GET", payload=None):
                if path == "git/ref/heads/automation/monthly-pin-bump-2026-10":
                    return {"object": {"sha": "candidate-sha"}}
                if path == "pulls" and method == "POST":
                    self.assertIn("Existing required CI gate: [passed]", payload["body"])
                    return {"number": 8, "html_url": "https://example.invalid/pull/8"}
                if path == "pulls/8":
                    return {"head": {"sha": "candidate-sha"}}
                raise AssertionError((path, method))
            with mock.patch.object(pins, "find_monthly_prs", return_value=[]), \
                 mock.patch.object(pins, "dispatch_gate",
                                   return_value=("https://example.invalid/gate/9", "success")), \
                 mock.patch.object(pins, "api_request", side_effect=api):
                result = pins.publish_pr("owner/repo", "automation/monthly-pin-bump-2026-10",
                                         "candidate-sha", str(report))
            self.assertIn("https://example.invalid/pull/8", result)
            self.assertIn("https://example.invalid/gate/9", result)
            self.assertIn("candidate-sha", result)

    def test_python_312_selection_skips_release_candidate_only_folder(self) -> None:
        root = '<a href="3.12.16/">3.12.16/</a><a href="3.12.15/">3.12.15/</a>'
        dirs = {
            "3.12.16": '<a href="Python-3.12.16rc2.tar.xz">rc</a>',
            "3.12.15": '<a href="Python-3.12.15.tar.xz">final</a>',
        }
        self.assertEqual(pins.select_python_312_final(root, dirs.__getitem__), "3.12.15")
        with self.assertRaises(pins.ResolveError):
            pins.select_python_312_final(root, lambda _version: "<html></html>", candidate_cap=2)


    def test_find_monthly_pr_searches_later_pages_and_does_not_infer_at_cap(self) -> None:
        target = {"number": 88, "html_url": "https://example.invalid/pull/88",
                  "head": {"ref": "automation/monthly-pin-bump-2026-10",
                           "user": {"login": "owner"}}}
        requested = []
        def api(_repo: str, path: str, method: str = "GET", payload=None):
            page = int(path.rsplit("page=", 1)[1])
            requested.append(page)
            return ([{}] * 100) if page < 5 else [target]
        with mock.patch.object(pins, "api_request", side_effect=api):
            self.assertIs(pins.find_monthly_pr("owner/repo"), target)
        self.assertEqual(requested, [1, 2, 3, 4, 5])

        def capped(_repo: str, _path: str, method: str = "GET", payload=None):
            return [{}] * 100
        with mock.patch.object(pins, "api_request", side_effect=capped):
            with self.assertRaisesRegex(pins.ResolveError, "safety cap"):
                pins.find_monthly_pr("owner/repo")


    def test_publish_rechecks_for_a_different_monthly_pr_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.md"
            report.write_text("Resolved cohort.\\n", encoding="utf-8")
            other = {"number": 91, "head": {"ref": "automation/monthly-pin-bump-2026-09"}}
            api = mock.Mock(return_value={"object": {"sha": "candidate-sha"}})
            with mock.patch.object(pins, "find_monthly_prs", side_effect=[[], [other]]), \
                 mock.patch.object(pins, "dispatch_gate",
                                   return_value=("https://example.invalid/gate/1", "success")), \
                 mock.patch.object(pins, "api_request", api):
                with self.assertRaisesRegex(pins.ResolveError, "opened during gate"):
                    pins.publish_pr("owner/repo", "automation/monthly-pin-bump-2026-10",
                                    "candidate-sha", str(report))
            self.assertFalse(any(call.args[1] == "pulls" and call.args[2] == "POST"
                                 for call in api.call_args_list))


NOW = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone.utc)


def packument(latest: str, ages: dict[str, int]) -> dict:
    """Registry metadata for versions published the given number of days before NOW."""
    return {"dist-tags": {"latest": latest},
            "versions": {version: {} for version in ages},
            "time": {"created": "2020-01-01T00:00:00.000Z", "modified": "2026-10-25T00:00:00.000Z",
                     **{version: (NOW - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")
                        for version, days in ages.items()}}}


class CooldownTests(unittest.TestCase):
    def test_npm_candidate_is_newest_stable_release_older_than_seven_days(self) -> None:
        metadata = packument("1.3.0", {"1.0.0": 40, "1.1.0": 8, "1.2.0": 6, "1.3.0": 1,
                                       "1.4.0-beta.1": 30, "2.0.0": 20})
        with mock.patch.object(pins, "request_json", return_value=metadata) as request:
            chosen, skipped = pins.npm_cooldown_candidate("@scope/tool", pins.cooldown_cutoff(NOW))
        request.assert_called_once_with("https://registry.npmjs.org/@scope%2ftool")
        self.assertEqual(chosen, "1.1.0")
        self.assertEqual(skipped, ["1.2.0", "1.3.0"])

    def test_npm_candidate_fails_when_every_release_is_young(self) -> None:
        with mock.patch.object(pins, "request_json", return_value=packument("1.0.0", {"1.0.0": 2})):
            with self.assertRaisesRegex(pins.ResolveError, "no tool release is older than the 7-day cooldown"):
                pins.npm_cooldown_candidate("tool", pins.cooldown_cutoff(NOW))

    def test_npm_bump_passes_cooldown_to_lock_and_names_skipped_candidate(self) -> None:
        metadata = {"@openai/codex": packument("0.9.0", {"0.8.0": 10, "0.9.0": 3}),
                    "ccusage": packument("1.0.0", {"1.0.0": 30})}
        lock = {"lockfileVersion": 2, "packages": {"": {}, "node_modules/x": {
            "integrity": "sha512-fixture", "resolved": "https://registry.npmjs.org/x/-/x.tgz"}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            npm_dir = root / "home/dot_local/share/bootstrap/npm"
            npm_dir.mkdir(parents=True)
            (npm_dir / "package.json").write_text(json.dumps(
                {"dependencies": {"@openai/codex": "0.7.0", "ccusage": "1.0.0"}}), encoding="utf-8")
            (npm_dir / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
            run = mock.Mock(return_value=mock.Mock(returncode=0))
            staged: dict[str, str] = {}
            notes: list[str] = []
            with mock.patch.object(pins, "request_json",
                                   side_effect=lambda url: metadata[url.rsplit("/", 1)[1].replace("%2f", "/")]), \
                 mock.patch.object(pins.subprocess, "run", run):
                pins.update_npm(root, staged, notes, NOW)
        install = run.call_args_list[0].args[0]
        self.assertEqual(install[:2], ["npm", "install"])
        self.assertIn("--before=2026-10-18T12:00:00Z", install)
        self.assertIn("Skipped young npm candidate @openai/codex 0.9.0: published within the 7-day cooldown.",
                      notes)
        self.assertIn("Updated npm tool @openai/codex to 0.8.0.", notes)
        package = json.loads(staged[str(npm_dir / "package.json")])
        self.assertEqual(package["dependencies"], {"@openai/codex": "0.8.0", "ccusage": "1.0.0"})

    def test_python_locks_rerun_their_compile_command_with_exclude_newer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in pins.PYTHON_LOCKS:
                path = root / relative
                path.parent.mkdir(parents=True)
                command = (f"uv pip compile {relative[:-5]}.in --python-version 3.12 "
                           f"--generate-hashes --output-file {relative}")
                path.write_text("# This file was autogenerated by uv via the following command:\n"
                                f"#    {command}\nold==1.0 \\\n    --hash=sha256:aa\n", encoding="utf-8")
            calls = []

            def compile_lock(args, cwd=None, env=None, timeout=900):
                calls.append((args, cwd))
                output = Path(args[args.index("--output-file") + 1])
                output.write_text("# regenerated\n", encoding="utf-8")
                if len(calls) == 2:
                    raise pins.ResolveError("required resolver command returned a failure")
                return ""

            staged: dict[str, str] = {}
            notes: list[str] = []
            with mock.patch.object(pins, "run_quietly", side_effect=compile_lock):
                pins.update_python_locks(root, staged, notes, NOW)
        first, cwd = calls[0]
        self.assertEqual(cwd, root)
        self.assertEqual(first[:3], ["uv", "pip", "compile"])
        self.assertEqual(first[first.index("--exclude-newer") + 1], "2026-10-18T12:00:00Z")
        self.assertIn("--upgrade", first)
        header = first[first.index("--custom-compile-command") + 1]
        self.assertTrue(header.endswith("--output-file " + pins.PYTHON_LOCKS[0]))
        self.assertEqual(list(staged), [str(root / pins.PYTHON_LOCKS[0])])
        self.assertIn(f"Refreshed Python lock {pins.PYTHON_LOCKS[0]} with uv --exclude-newer "
                      "2026-10-18T12:00:00Z.", notes)
        self.assertTrue(any(note.startswith(f"Retained Python lock {pins.PYTHON_LOCKS[1]}") for note in notes))


if __name__ == "__main__":
    unittest.main()
