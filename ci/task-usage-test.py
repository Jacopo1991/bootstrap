#!/usr/bin/env python3
"""Usage readers, policy, pace verdicts and pace gate, on fixture readings in a temporary HOME.

Never reads the real ~/.codex, ~/.claude or ~/.local/state: every path comes from the temporary HOME.
"""
import json
import os
import shutil
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "home/dot_local/bin"))
sys.dont_write_bytecode = True
import task_usage as usage  # noqa: E402

FIXTURES = ROOT / "ci/fixtures/usage"
NOW = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)  # a minute-level hour after the Codex fixture
FIVE_LEFT = timedelta(hours=5)  # a whole 5-hour window left: the full 5-hour reserve applies
DEAD = 2 ** 22 + 7  # above the largest pid, so never alive


def epoch(when):
    return int(when.timestamp())


class Usage(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="usage-test-")))

    def claude(self, used, left, five=0.1, age=timedelta(minutes=5), five_left=FIVE_LEFT):
        """Write the state a recorded stream leaves: used is a percent, left hours until the weekly reset."""
        reset = NOW + timedelta(hours=left)
        event = {"rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": five, "resetsAt": epoch(NOW + five_left)},
            "seven_day": {"utilization": used / 100, "resetsAt": epoch(reset)}}}}
        usage.record_claude(self.home, event, NOW - age)

    def codex(self, text=None, timestamp=None):
        path = self.home / ".codex/sessions/2026/10/20/rollout-2026-10-20T19-00-00-abc.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text is not None else (FIXTURES / "codex-rollout-sample.jsonl").read_text())

    def verdict(self, vendor="claude", now=NOW):
        return usage.assessments(self.home, now)[1][vendor]  # as the gate sees it, lanes that ran included

    def test_codex_reader_takes_the_newest_rate_limits_of_the_fixture(self):
        self.codex()
        reading = usage.read_codex(self.home, NOW)
        self.assertEqual((reading["weekly"], reading["five_hour"]), (75.0, None))
        self.assertEqual(reading["at"], datetime(2026, 10, 20, 19, 13, 28, 537000, tzinfo=timezone.utc))
        self.assertEqual(reading["resets_at"], datetime.fromtimestamp(1793082750, timezone.utc))

    def test_codex_reader_reads_primary_five_hour_and_secondary_weekly(self):
        line = json.loads((FIXTURES / "codex-rollout-sample.jsonl").read_text().splitlines()[0])
        line["payload"]["rate_limits"] = {
            "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": epoch(NOW + timedelta(hours=3))},
            "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": 1793082750}}
        self.codex(json.dumps(line) + "\n")
        reading = usage.read_codex(self.home, NOW)
        self.assertEqual((reading["weekly"], reading["five_hour"]), (40.0, 12.0))

    def test_claude_reader_takes_both_windows_of_the_fixture_event(self):
        event = json.loads((FIXTURES / "claude-rate-limit-event.json").read_text())
        at = datetime.fromtimestamp(1792188000, timezone.utc) - timedelta(hours=10)
        usage.record_claude(self.home, event, at)
        reading = usage.read_claude(self.home, at + timedelta(minutes=3))
        self.assertEqual((reading["weekly"], reading["five_hour"]), (85.0, 16.0))
        self.assertEqual(reading["at"], at)
        self.assertEqual(reading["resets_at"], datetime.fromtimestamp(1792188000, timezone.utc))

    def test_no_reading_and_stale_readings_are_unknown_with_a_reason(self):
        self.assertIn("no rollout file", usage.read_codex(self.home, NOW)["reason"])
        self.assertIn("no rate_limit_event", usage.read_claude(self.home, NOW)["reason"])
        self.claude(50, 80, age=timedelta(days=8))
        self.assertIn("older than the 7-day window", usage.read_claude(self.home, NOW)["reason"])
        self.claude(50, 80)
        later = NOW + timedelta(hours=81)
        self.assertIn("before the last weekly reset", usage.read_claude(self.home, later)["reason"])
        self.codex()
        self.assertIn("before the last weekly reset", usage.read_codex(self.home, NOW + timedelta(days=8))["reason"])
        self.assertEqual(self.verdict("claude", later)["verdict"], "unknown")

    def test_every_pace_verdict(self):
        # reserve 25 -> automated share ends at 75 %; margin 10 points; elapsed 50 % at 84 h left.
        cases = [(50, 84, "on"), (30, 84, "under"), (70, 84, "ahead"), (80, 84, "exhausted-automated-share"),
                 (40, 30, "under-used-near-reset"), (74, 30, "on")]
        for used, left, expected in cases:
            with self.subTest(used=used, left=left):
                self.claude(used, left)
                found = self.verdict()
                self.assertEqual(found["verdict"], expected)
                self.assertAlmostEqual(found["elapsed"], 100 * (1 - left / 168))

    def test_spare_reset_held_has_no_reserve_and_never_holds_for_pace(self):
        self.codex()
        found = self.verdict("codex")  # 75 % used, 8 % of the week gone: ahead, but a spare reset is held
        self.assertEqual((found["verdict"], found["spare"], found["limit"]), ("ahead", True, 100))
        self.assertIn(usage.SPARE, "\n".join(usage.report(self.home, NOW)))
        decision = usage.gate(self.home, "lane", "codex", "routine", "proj", "t1", NOW)
        self.assertEqual((decision["hold"], decision["cheaper"], decision["vendor"]), ("", False, "codex"))

    def test_policy_defaults_pointer_override_and_errors(self):
        defaults = usage.policy(self.home)
        self.assertEqual((defaults["reserve"], defaults["spare_resets"]),
                         ({"claude": 25, "codex": 10}, {"claude": 0, "codex": 1}))
        self.assertEqual(defaults["priority"], ["madplanner-v2", "household-signal-harness",
                                                 "product-harness", "cortex-kb-company"])
        self.assertEqual((defaults["under_use_percent"], defaults["under_use_hours"], defaults["ahead_margin"]),
                         (60, 36, 10))
        policy = self.home / "cortex/cortex-kb-company/policy/usage-pacing.toml"
        policy.parent.mkdir(parents=True)
        policy.write_text('ahead_margin = 5\npriority = ["a"]\n[reserve]\nclaude = 40\n[spare_resets]\ncodex = 0\n')
        found = usage.policy(self.home)
        self.assertEqual((found["ahead_margin"], found["priority"], found["reserve"], found["spare_resets"]),
                         (5, ["a"], {"claude": 40, "codex": 10}, {"claude": 0, "codex": 0}))
        other = self.home / "elsewhere.toml"
        other.write_text("under_use_hours = 12\n")
        (self.home / ".config/task").mkdir(parents=True)
        (self.home / ".config/task/usage-policy-path").write_text("~/elsewhere.toml\n")
        self.assertEqual(usage.policy(self.home)["under_use_hours"], 12)
        self.assertEqual(usage.policy(self.home)["ahead_margin"], 10)  # the pointed-to file replaces the default one
        other.write_text("reserve = 5\nahead_margin = 4\n")
        warnings = []
        found = usage.policy(self.home, warnings)  # an invalid key keeps its default and warns
        self.assertEqual((found["reserve"], found["ahead_margin"]), (usage.DEFAULTS["reserve"], 4))
        self.assertEqual(len(warnings), 1)
        self.assertIn("invalid reserve; ignored, other keys kept", warnings[0])
        decision = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertFalse(any("built-in defaults used" in note for note in decision["notes"]))
        self.assertTrue(any("invalid reserve" in note for note in decision["notes"]))
        other.write_text("reserve = [\n")  # an unparseable file still falls back with a warning
        with self.assertRaises(ValueError):
            usage.policy(self.home)
        decision = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertTrue(any("built-in defaults used" in note for note in decision["notes"]))

    def test_example_policy_file_parses_and_matches_the_defaults(self):
        example = tomllib.loads((ROOT / "docs/usage-pacing.example.toml").read_text())
        pointer = self.home / ".config/task/usage-policy-path"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(str(ROOT / "docs/usage-pacing.example.toml"))
        found = usage.policy(self.home)
        self.assertEqual({key: found[key] for key in example}, example)
        self.assertEqual({key: value for key, value in found.items() if key != "research_backlog"},
                         {key: value for key, value in usage.DEFAULTS.items() if key != "research_backlog"})

    def test_review_policy_company_path_defaults_and_partial_overrides(self):
        defaults = usage.policy(self.home)["review"]
        self.assertEqual(defaults["low"], dict(max_rounds=2, extra_rounds=1))
        self.assertEqual(defaults["normal"], dict(codex="gpt-6.1-sol", claude="gpt-6-luna", max_rounds=3, extra_rounds=1))
        self.assertEqual((defaults["high"]["codex"], defaults["high"]["claude"]), ("sonnet", "gpt-6.1-sol"))  # decision-32
        self.assertEqual(defaults["high"]["red_team_model"], "gpt-6.1-sol")
        self.assertEqual(defaults["high"]["red_team_model_claude"], "sonnet")
        self.assertIn("system/agent-policy/**", defaults["high_paths"]["bootstrap"])
        self.assertIn("home/dot_local/share/bootstrap/agent-policy/**", defaults["high_paths"]["bootstrap"])
        self.assertNotIn("home/dot_local/bin/executable_task", defaults["high_paths"]["bootstrap"])
        path = self.home / "cortex/cortex-kb-company/policy/usage-pacing.toml"
        path.parent.mkdir(parents=True)
        path.write_text('[review]\nhigh_paths = { proj = ["rails/**"] }\n'
                        '[review.normal]\ncodex = "sonnet"\nmax_rounds = 4\n'
                        '[review.high]\nred_team_model = "gpt-6-luna"\nred_team_model_claude = "haiku"\n')
        found = usage.policy(self.home)["review"]
        self.assertEqual(found["normal"], dict(codex="sonnet", claude="gpt-6-luna", max_rounds=4, extra_rounds=1))
        self.assertEqual(found["high"]["red_team_model"], "gpt-6-luna")
        self.assertEqual(found["high"]["red_team_model_claude"], "haiku")
        self.assertEqual(found["high_paths"]["proj"], ["rails/**"])
        self.assertEqual(found["high_paths"]["*"], defaults["high_paths"]["*"])
        self.assertEqual(usage.DEFAULTS["review"]["normal"]["max_rounds"], 3)

    def test_review_policy_rejects_unknown_keys_and_invalid_types(self):
        path = self.home / "cortex/cortex-kb-company/policy/usage-pacing.toml"
        path.parent.mkdir(parents=True)
        for text in ('review = 1', '[review]\nbogus = {}', '[review.normal]\nbogus = 1',
                     '[review.low]\ncodex = "haiku"', '[review.normal]\nmax_rounds = 0',
                     '[review.high]\nmax_rounds = true', '[review.high]\nred_team_model_claude = 3',
                     '[review.low]\nextra_rounds = -1',
                     '[review.high]\nextra_rounds = 1.5', '[review.normal]\ncodex = ""',
                     '[review.normal]\nclaude = "x; touch file"', '[review.high]\nred_team_model = 3',
                     '[review]\nhigh_paths = []', '[review]\nhigh_paths = { proj = [1] }',
                     '[review]\nhigh_paths = { proj = [""] }', '[review]\nnormal = []'):
            with self.subTest(text=text):
                path.write_text('ahead_margin = 4\n' + text + '\n')
                pol, _, notes = usage.assessments(self.home, NOW)
                self.assertEqual(pol, dict(usage.DEFAULTS, ahead_margin=4))  # review keeps its defaults, the rest is kept
                policy_notes = [note for note in notes if 'usage-pacing.toml' in note]
                self.assertEqual(len(policy_notes), 1)
                self.assertRegex(policy_notes[0], 'invalid review.*other keys kept')

    def write_policy(self, text):
        path = self.home / "cortex/cortex-kb-company/policy/usage-pacing.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def codex_week(self, used, started, five=None, at=NOW - timedelta(minutes=5)):
        """A Codex reading `started` into its weekly window (the window starts at resets_at - 7 days)."""
        limits = {"primary": {"used_percent": used, "window_minutes": 10080, "resets_at": epoch(NOW + usage.WEEK - started)}}
        if five is not None:
            limits["secondary"] = {"used_percent": five, "window_minutes": 300, "resets_at": epoch(NOW + FIVE_LEFT)}
        self.codex(json.dumps({"timestamp": at.isoformat(), "payload": {"rate_limits": limits}}) + "\n")

    RAMP = "codex_ramp = { per_day = 20, ceiling = 80 }\n"

    def test_codex_ramp_day_allowance_and_ceiling(self):
        self.write_policy(self.RAMP)
        for started, day, allowance in ((timedelta(hours=2), 1, 20), (timedelta(days=2, hours=1), 3, 60),
                                        (timedelta(days=4, hours=1), 5, 80), (timedelta(days=6, hours=23), 7, 80)):
            with self.subTest(day=day):
                self.codex_week(allowance, started)  # at the allowance: still allowed
                found = self.verdict("codex")["ramp"]
                self.assertEqual((found["day"], found["allowance"], found["over"]), (day, allowance, False))
                self.assertEqual(usage.gate(self.home, "lane", "codex", "routine", "proj", "t1", NOW)["hold"], "")
                self.assertIn(f"  ramp: day {day}, allowance {allowance} % (20 %/day, ceiling 80 %); within ramp",
                              usage.report(self.home, NOW))
                self.codex_week(allowance + 1, started)
                self.assertTrue(self.verdict("codex")["ramp"]["over"])
                self.assertIn("past ramp", "\n".join(usage.report(self.home, NOW)))

    def test_codex_ramp_early_reset_restarts_at_day_one(self):
        self.write_policy(self.RAMP)
        self.codex_week(50, timedelta(days=4, hours=1))
        self.assertEqual(self.verdict("codex")["ramp"]["day"], 5)
        self.codex_week(25, timedelta(hours=1))  # the reset came early: resets_at moved, the window restarts
        found = self.verdict("codex")["ramp"]
        self.assertEqual((found["day"], found["allowance"], found["over"]), (1, 20, True))

    def test_codex_ramp_refuses_with_values_and_exemption_launches(self):
        self.write_policy(self.RAMP)
        self.codex_week(45, timedelta(days=1, hours=3))  # day 2: allowance 40
        decision = usage.gate(self.home, "review", "codex", "routine", "proj", "t1", NOW)
        self.assertIn("codex past its daily ramp: weekly used 45 % > allowance 40 % (day 2, 20 %/day, ceiling 80 %); "
                      "no new reviews on codex", decision["hold"])
        self.assertEqual(decision["ramp_exempt"], "")
        self.assertIn("held review proj/t1: codex past its daily ramp", (usage._dir(self.home) / "pace.log").read_text())
        self.codex_week(95, timedelta(days=1, hours=3))  # above the ceiling too
        decision = usage.gate(self.home, "lane", "codex", "routine", "proj", "t1", NOW, exempt="security")
        self.assertEqual((decision["hold"], decision["ramp_exempt"]), ("", "security"))
        self.assertTrue(any("ramp-exempt security" in note for note in decision["notes"]))
        self.assertEqual(usage.ramp_gate(self.home, "red-team", "codex", "proj", "t1", "", NOW)[0][:25], "codex past its daily ramp")
        self.assertEqual(usage.ramp_gate(self.home, "red-team", "codex", "proj", "t1", "money", NOW), ("", "money"))

    def test_codex_ramp_routine_lane_does_not_move_to_codex_past_its_ramp(self):
        self.write_policy(self.RAMP)
        self.claude(80, 84)
        self.codex_week(30, timedelta(hours=2))
        decision = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertEqual(decision["vendor"], "claude")
        self.assertIn("claude automated share used up", decision["hold"])

    def test_codex_ramp_leaves_claude_unchanged(self):
        self.write_policy(self.RAMP)
        self.claude(50, 168 - 2)  # 50 % on day 1 of Claude's week would be far past a 20 %/day ramp
        found = self.verdict("claude")
        self.assertIsNone(found["ramp"])
        decision = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertNotIn("ramp", decision["hold"])
        self.assertFalse(any("ramp" in line for line in usage.report(self.home, NOW) if line.startswith("claude")))

    def test_no_ramp_policy_means_no_ramp(self):
        self.codex_week(90, timedelta(hours=2))
        self.assertIsNone(self.verdict("codex")["ramp"])
        self.assertEqual(usage.gate(self.home, "lane", "codex", "routine", "proj", "t1", NOW)["hold"], "")

    def test_invalid_codex_ramp_and_unknown_key_are_warned_and_the_rest_kept(self):
        for text in ("codex_ramp = 5", "codex_ramp = { per_day = 20 }", "codex_ramp = { per_day = 20, ceiling = 120 }",
                     "codex_ramp = { per_day = 0, ceiling = 80 }", 'codex_ramp = { per_day = "20", ceiling = 80 }',
                     "codex_ramp = { per_day = 20, ceiling = 80, extra = 1 }"):
            with self.subTest(text=text):
                warnings = []
                self.write_policy(f"ahead_margin = 4\n{text}\n")
                found = usage.policy(self.home, warnings)
                self.assertEqual((found["codex_ramp"], found["ahead_margin"]), (None, 4))
                self.assertEqual(len(warnings), 1)
                self.assertIn("invalid codex_ramp; ignored, other keys kept", warnings[0])
        self.write_policy('bogus = 1\nahead_margin = 4\n[review.normal]\nmax_rounds = 5\n')
        warnings = []
        found = usage.policy(self.home, warnings)
        self.assertEqual((found["ahead_margin"], found["review"]["normal"]["max_rounds"]), (4, 5))
        self.assertNotIn("bogus", found)
        self.assertEqual(len(warnings), 1)
        self.assertIn("unknown key bogus; ignored, other keys kept", warnings[0])
        self.assertIn("unknown key bogus", "\n".join(usage.report(self.home, NOW)))
        self.write_policy('[review]\nhigh_paths = { proj = ["x/**"] }\n[review.low]\nmax_rounds = 0\n')
        warnings = []
        found = usage.policy(self.home, warnings)["review"]
        self.assertEqual((found["high_paths"]["proj"], found["low"]["max_rounds"]), (["x/**"], 2))  # only the bad table is dropped
        self.assertEqual(len(warnings), 1)
        self.assertIn("invalid review.low.max_rounds; ignored, other keys kept", warnings[0])
        self.write_policy('blocked = { gemini = "x" }\n')
        warnings = []
        self.assertEqual(usage.policy(self.home, warnings)["blocked"], {})
        self.assertIn("invalid blocked", warnings[0])

    def fallback(self):
        lines = usage.report(self.home, NOW)
        return lines[lines.index("fallback:") + 1:] if "fallback:" in lines else None

    def test_fallback_block_is_absent_when_every_vendor_is_normal(self):
        self.write_policy(self.RAMP)
        self.claude(50, 84)
        self.codex_week(10, timedelta(hours=2))
        self.assertIsNone(self.fallback())

    def test_fallback_block_for_each_state(self):
        self.write_policy(self.RAMP)
        self.claude(50, 84)
        self.codex_week(25, timedelta(hours=2))
        self.assertEqual(self.fallback(), ["  codex past ramp (day 1, 20 %): builds -> Claude lane; reviews of Claude work -> wait"])
        self.codex_week(10, timedelta(hours=2))
        self.claude(80, 84)
        self.assertEqual(self.fallback(), ["  claude braked (automated share used up): builds -> Codex lane; reviews of Codex work -> wait"])
        self.claude(50, 84, five=.65)  # reserve 25: warning band from 60 %
        self.assertEqual(self.fallback(), ["  claude near its 5-hour window: builds -> Codex lane; reviews of Codex work -> one at a time"])
        self.claude(50, 84)
        self.write_policy(self.RAMP + 'blocked = { codex = "sandbox defect T-12" }\n')
        self.assertEqual(self.fallback(), ["  codex blocked by a foundation defect (sandbox defect T-12): "
                                           "builds -> Claude lane; reviews of Claude work -> wait"])
        self.claude(80, 84)  # both vendors out: no vendor to fall back to for builds
        self.assertEqual(self.fallback(), [
            "  claude braked (automated share used up): builds -> wait; reviews of Codex work -> wait",
            "  codex blocked by a foundation defect (sandbox defect T-12): builds -> wait; reviews of Claude work -> wait"])

    def test_gate_unknown_reading_warns_and_changes_nothing(self):
        decision = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertEqual((decision["vendor"], decision["hold"], decision["cheaper"], decision["spend"]),
                         ("claude", "", False, ""))
        self.assertTrue(any("pace warning: claude usage unknown" in note for note in decision["notes"]))

    def test_gate_on_and_under_pace_are_unchanged(self):
        for used in (50, 30):
            self.claude(used, 84)
            decision = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
            self.assertEqual((decision["vendor"], decision["hold"], decision["cheaper"], decision["spend"]),
                             ("claude", "", False, ""))

    def test_gate_ahead_uses_cheaper_model_and_only_the_top_priority_repo_gets_lanes(self):
        self.claude(70, 84)
        top = self.home / ".local/state/task/madplanner-v2/t1"
        top.mkdir(parents=True)
        (top / "task.json").write_text("{}")
        held = usage.gate(self.home, "lane", "claude", "routine", "proj", "t5", NOW)
        self.assertIn("only madplanner-v2 gets new lanes", held["hold"])
        self.assertTrue(held["cheaper"])
        allowed = usage.gate(self.home, "lane", "claude", "hard", "madplanner-v2", "t2", NOW)
        self.assertEqual((allowed["hold"], allowed["cheaper"]), ("", False))
        (top / "merge.json").write_text("{}")  # merged: madplanner-v2 no longer has work
        self.assertEqual(usage.gate(self.home, "lane", "claude", "routine", "proj", "t5", NOW)["hold"], "")
        self.assertEqual(usage.gate(self.home, "review", "claude", "routine", "proj", "t5", NOW)["hold"], "")

    def test_gate_exhausted_switches_routine_lanes_or_holds(self):
        self.claude(80, 84)
        self.codex()
        # Codex 75 % used, 8 % of the week gone, spare reset held: not held back for pace, so it takes the lane.
        moved = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertEqual((moved["vendor"], moved["hold"]), ("codex", ""))
        self.assertTrue(any("moves to codex" in note for note in moved["notes"]))
        for kind, tier in (("lane", "hard"), ("review", "routine")):
            held = usage.gate(self.home, kind, "claude", tier, "proj", "t1", NOW)
            self.assertEqual(held["vendor"], "claude")
            self.assertIn("automated share used up", held["hold"])

    def test_gate_exhausted_everywhere_waits_and_asks_for_the_spare_reset(self):
        self.claude(80, 84)
        self.codex(text=(FIXTURES / "codex-rollout-sample.jsonl").read_text().replace('"used_percent":75.0', '"used_percent":100.0'))
        held = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertIn("automated share used up", held["hold"])
        again = usage.gate(self.home, "lane", "codex", "routine", "proj", "t2", NOW)
        self.assertTrue(any("use your spare reset" in note for note in again["notes"]))
        self.assertIn("automated share used up", again["hold"])

    def test_gate_under_used_near_reset_asks_for_the_boost_until_the_reset(self):
        self.claude(40, 30)
        decision = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertEqual(decision["spend"], (NOW + timedelta(hours=30)).isoformat())
        text = "\n".join(usage.report(self.home, NOW))
        self.assertIn("pace: under-used-near-reset", text)
        self.assertIn("spend before the reset", text)

    def test_boost_threshold_policy_default_override_and_validation(self):
        self.assertEqual(usage.policy(self.home)["boost_five_hour_max"], 50)
        path = self.home / "cortex/cortex-kb-company/policy/usage-pacing.toml"
        path.parent.mkdir(parents=True)
        for value in (0, 35.5, 100):
            path.write_text(f"boost_five_hour_max = {value}\n")
            self.assertEqual(usage.policy(self.home)["boost_five_hour_max"], value)
        for value in ("-1", "101", "true", '"50"', "nan", "inf"):
            with self.subTest(value=value):
                path.write_text(f"boost_five_hour_max = {value}\n")
                warnings = []
                self.assertEqual(usage.policy(self.home, warnings)["boost_five_hour_max"], 50)
                self.assertRegex(warnings[0], "invalid boost_five_hour_max; ignored, other keys kept")
        path.write_text("boost_five_hour_max = 35.5\n")
        self.claude(40, 30, five=.36)
        self.assertIn("36 % ≥ 35.5 %", usage.boost_pauses(self.home, NOW)["claude"])

    def test_automatic_boost_requires_room_in_an_existing_five_hour_window(self):
        for vendor in usage.VENDORS:
            for five, age, allowed in ((49.9, 0, True), (50, 0, False), (62, 0, False), (10, 46, False)):
                with self.subTest(vendor=vendor, five=five, age=age):
                    self.five(vendor, five, week=40, left=30, age=timedelta(minutes=age))
                    self.busy(vendor, at=NOW - timedelta(minutes=30), pid=DEAD)
                    decision = usage.gate(self.home, "review", vendor, "routine", "proj", "t1", NOW)
                    self.assertEqual(bool(decision["spend"]), allowed)
                    self.assertEqual(vendor not in usage.boost_pauses(self.home, NOW), allowed)
        self.claude(40, 30, five_left=timedelta(hours=-1))
        self.codex(json.dumps({'timestamp': NOW.isoformat(), 'payload': {'rate_limits': {
            'primary': {'used_percent': 40, 'window_minutes': 10080,
                        'resets_at': epoch(NOW + timedelta(hours=30))}}}}) + '\n')
        self.assertEqual(usage.gate(self.home, "lane", "claude", "hard", "proj", "t1", NOW)["spend"], "")
        self.assertIn("no current 5-hour reading", usage.boost_pauses(self.home, NOW)["claude"])
        self.assertEqual(usage.gate(self.home, "lane", "codex", "hard", "proj", "t1", NOW)["spend"],
                         (NOW + timedelta(hours=30)).isoformat())
        self.assertNotIn("codex", usage.boost_pauses(self.home, NOW))

    def test_boost_stale_rule_matches_warning_band_after_idle_and_lanes(self):
        for vendor in usage.VENDORS:
            self.five(vendor, 10, age=timedelta(minutes=46), week=40, left=30)
            self.busy(vendor, at=NOW - timedelta(hours=1), pid=DEAD)
            self.assertEqual(self.verdict(vendor)["boost_pause"], "")  # no lane since reading
            self.assertTrue(usage.gate(self.home, "lane", vendor, "hard", "p", "t", NOW)["spend"])
            self.busy(vendor, at=NOW - timedelta(minutes=30), pid=DEAD)
            self.assertTrue(self.verdict(vendor)["stale"])
            self.assertIn("stale", usage.boost_pauses(self.home, NOW)[vendor])
            self.assertEqual(usage.gate(self.home, "lane", vendor, "hard", "p", "t", NOW)["spend"], "")
            self.five(vendor, 10, age=timedelta(minutes=44))
            self.assertEqual(self.verdict(vendor)["boost_pause"], "")
        # A new weekly event changes the warning band's age too; boost uses exactly that rule.
        self.claude(40, 30, age=timedelta(minutes=46))
        usage.record_claude(self.home, {"rate_limit_info": {"rateLimitType": "seven_day", "utilization": .4,
                            "resetsAt": epoch(NOW + timedelta(hours=30))}}, NOW)
        self.assertFalse(self.verdict()["stale"])
        self.assertNotIn("claude", usage.boost_pauses(self.home, NOW))

    def test_usage_reports_pause_without_claiming_boost_is_on(self):
        self.claude(40, 30, five=.62)
        text = "\n".join(usage.report(self.home, NOW))
        self.assertIn("boost paused: claude 5-hour 62 % ≥ 50 %", text)
        self.assertNotIn("boost on until", text)

    def test_usage_pause_requires_a_saved_or_automatic_boost(self):
        self.claude(50, 84, five=.62)
        self.assertNotIn("boost paused:", "\n".join(usage.report(self.home, NOW)))
        path = self.home / ".local/state/task/boost.json"
        path.write_text(json.dumps({"codex": "2099-01-01T00:00:00+00:00"}))
        self.assertNotIn("boost paused:", "\n".join(usage.report(self.home, NOW)))
        path.write_text(json.dumps({"claude": "2099-01-01T00:00:00+00:00"}))
        self.assertIn("boost paused: claude", "\n".join(usage.report(self.home, NOW)))
        path.write_text(json.dumps({"claude": "2000-01-01T00:00:00+00:00"}))
        self.assertNotIn("boost paused:", "\n".join(usage.report(self.home, NOW)))
        self.claude(40, 30, five=.62)
        self.assertIn("boost paused: claude", "\n".join(usage.report(self.home, NOW)))

    def release_policy(self):
        policy = self.home / "cortex/cortex-kb-company/policy/usage-pacing.toml"
        policy.parent.mkdir(parents=True)
        policy.write_text("under_use_percent = 95\nunder_use_hours = 24\n[reserve]\nclaude = 30\n")

    def test_weekly_reserve_is_released_in_the_final_window_only(self):
        self.release_policy()
        for used, left, limit, expected in ((69, 30, 70, "under"),(72, 30, 70, usage.EXHAUSTED),
                                            (72, 20, 100, "under-used-near-reset"), (72, 24, 100, "under-used-near-reset")):
            with self.subTest(used=used, left=left):
                self.claude(used, left)
                found = self.verdict()
                self.assertEqual((found["limit"], found["verdict"], found["released"]), (limit, expected, limit == 100))
        self.claude(72, 30)
        self.assertIn("automated share used up (72 % of 70 %)", usage.gate(self.home, "lane", "claude", "hard", "p", "t", NOW)["hold"])
        self.claude(72, 20)
        decision = usage.gate(self.home, "lane", "claude", "hard", "p", "t", NOW)
        self.assertEqual((decision["hold"], decision["spend"]), ("", (NOW + timedelta(hours=20)).isoformat()))

    def test_five_hour_bands_keep_their_reserve_in_the_final_window(self):
        self.release_policy()
        for five, expected in ((54, "ok"), (55, "5h-warning"), (70, "5h-exhausted")):  # reserve 30: warning 55, exhausted 70
            with self.subTest(five=five):
                self.claude(72, 20, five=five / 100)
                found = self.verdict()
                self.assertEqual((found["limit"], found["five"]), (100, expected))
        self.claude(72, 20, five=0.7)
        self.assertIn("5-hour window used up (70 % of 70 %)", usage.gate(self.home, "lane", "claude", "hard", "p", "t", NOW)["hold"])

    def test_usage_says_when_the_reserve_is_released(self):
        self.release_policy()
        self.claude(72, 30)
        self.assertNotIn("reserve released", "\n".join(usage.report(self.home, NOW)))
        self.claude(72, 20)
        text = "\n".join(usage.report(self.home, NOW))
        self.assertIn(f"reserve released until {usage._when(NOW + timedelta(hours=20))}", text)
        self.assertIn("exhausted at 70 %", text)  # the 5-hour bands still show the reserve

    def test_usage_in_the_release_window_names_the_100_limit_even_when_it_is_used_up(self):
        self.release_policy()
        until = f"reserve released until {usage._when(NOW + timedelta(hours=20))}"
        for used in (72, 100):
            with self.subTest(used=used):
                self.claude(used, 20)
                text = "\n".join(usage.report(self.home, NOW))
                self.assertNotIn("reserve 30 %", text)
                self.assertIn(f"100 % limit used up, {until}" if used == 100 else until, text)
        self.assertEqual((self.verdict()["verdict"], self.verdict()["limit"]), (usage.EXHAUSTED, 100))
        self.assertIn("automated share used up (100 % of 100 %)", usage.gate(self.home, "lane", "claude", "hard", "p", "t", NOW)["hold"])
        self.claude(100, 30)  # outside the window the old wording stays
        self.assertIn("reserve 30 % used up", "\n".join(usage.report(self.home, NOW)))

    def test_a_vendor_with_a_spare_reset_gets_the_release_wording_first(self):
        self.release_policy()
        policy = self.home / "cortex/cortex-kb-company/policy/usage-pacing.toml"
        policy.write_text(policy.read_text() + "[spare_resets]\nclaude = 1\n")
        until = f"reserve released until {usage._when(NOW + timedelta(hours=20))}"
        for used, left, first in ((72, 20, f"100 % weekly limit, {until}; {usage.SPARE}"),
                                  (100, 20, f"100 % limit used up, {until}; {usage.SPARE}"), (72, 30, usage.SPARE)):
            with self.subTest(used=used, left=left):
                self.claude(used, left)
                self.assertTrue(self.verdict()["released"] == (left == 20))
                self.assertIn(f"policy: {first}", "\n".join(usage.report(self.home, NOW)))
        self.claude(72, 20)
        decision = usage.gate(self.home, "lane", "claude", "hard", "p", "t", NOW)
        self.assertEqual((decision["hold"], decision["spend"]), ("", (NOW + timedelta(hours=20)).isoformat()))

    def test_five_hour_reserve_scales_with_the_time_left_in_the_window(self):
        self.release_policy()  # reserve 30: full window exhausted at 70, warning at 55
        for hours, five, expected, out, warn in ((1.5, 62, "ok", 91, 76), (1.5, 76, "5h-warning", 91, 76), (1.5, 91, "5h-exhausted", 91, 76),
                                                 (4.5, 62, "5h-warning", 73, 58), (4.5, 57, "ok", 73, 58), (4.5, 73, "5h-exhausted", 73, 58),
                                                 (5, 62, "5h-warning", 70, 55), (7, 62, "5h-warning", 70, 55)):
            with self.subTest(hours=hours, five=five):
                self.claude(40, 100, five=five / 100, five_left=timedelta(hours=hours))
                found = self.verdict()
                self.assertEqual((found["five"], 100 - found["five_reserve"], 100 - found["five_reserve"] - usage.FIVE_BAND),
                                 (expected, out, warn))
        self.claude(40, 100, five=0.8, five_left=timedelta(hours=1.5))
        text = "\n".join(usage.report(self.home, NOW))
        self.assertIn("exhausted at 91 %, warning at 76 % (reserve 9 % of 30 %, scaled by the time left)", text)
        self.assertEqual(usage.gate(self.home, "lane", "claude", "hard", "p", "t", NOW)["hold"], "")
        self.claude(40, 100, five=0.95, five_left=timedelta(hours=1.5))
        self.assertIn("5-hour window used up (95 % of 91 %)", usage.gate(self.home, "lane", "claude", "hard", "p", "t", NOW)["hold"])

    def test_stale_and_missing_five_hour_readings_keep_the_full_reserve(self):
        self.release_policy()
        self.claude(40, 100, five=0.8, age=timedelta(minutes=46), five_left=timedelta(hours=1.5))
        self.assertEqual((self.verdict()["five"], self.verdict()["five_reserve"]), ("5h-warning", 9))  # no lane ran: scaled, 76 <= 80 < 91
        self.busy("claude", at=NOW - timedelta(minutes=30), pid=DEAD)  # a lane ran since the reading: stale
        found = self.verdict()
        self.assertEqual((found["five"], found["five_reserve"]), ("5h-exhausted", 30))  # 80 >= 70 on the full reserve
        event = {"rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": 0.8, "resetsAt": epoch(NOW - timedelta(hours=1))},
            "seven_day": {"utilization": 0.4, "resetsAt": epoch(NOW + timedelta(hours=100))}}}}
        usage.record_claude(self.home, event, NOW - timedelta(minutes=5))  # that window has reset: no reading
        found = self.verdict()
        self.assertEqual((found["five"], found["missing"], found["five_reserve"]), ("5h-warning", True, 30))
        self.assertEqual(self.verdict("codex")["five_reserve"], 10)  # no reading at all

    def test_batch_gate_refuses_ahead_vendors_only(self):
        self.claude(70, 84)
        reason, _ = usage.batch_gate(self.home, ["claude"], NOW)
        self.assertIn("claude is ahead of pace", reason)
        reason, notes = usage.batch_gate(self.home, ["codex"], NOW)  # codex unknown: unchanged plus a warning
        self.assertEqual(reason, "")
        self.assertTrue(any("codex usage unknown" in note for note in notes))

    def test_report_lines_and_times(self):
        self.claude(50, 84)
        text = "\n".join(usage.report(self.home, NOW))
        for expected in ("claude: weekly used 50 %, 5-hour used 10 %", "2026-10-24 08:00 UTC / 2026-10-24 10:00 Europe/Copenhagen",
                         "5 min old", "week elapsed 50 %", "pace: on", "reserve 25 %", "codex: unknown ("):
            self.assertIn(expected, text)

    def test_weekly_line_counts_runs_instead_of_pacing_holds(self):
        self.claude(80, 84)
        usage.gate(self.home, "lane", "claude", "hard", "proj", "t1", NOW)
        usage.gate(self.home, "lane", "claude", "hard", "proj", "t1", NOW)  # same lane again: counted once
        usage.gate(self.home, "review", "claude", "routine", "proj", "t2", NOW)
        lines = usage.weekly_lines(self.home, NOW)
        self.assertEqual(lines[0], "claude last 7d: runs 0, weekly usage delta None, outcomes {}")
        self.assertEqual(lines[1], "codex last 7d: runs 0, weekly usage delta None, outcomes {}")
        self.claude(30, 20)  # the next reading, in a later week: its own counters
        later = usage.weekly_lines(self.home, NOW)[0]
        self.assertEqual(later, lines[0])

    def test_weekly_line_lists_this_weeks_model_overrides_only(self):
        self.assertEqual(usage.weekly_lines(self.home, NOW)[-1], "model overrides, last 7 days: 0")
        usage.record_override(self.home, "proj", "T-1", "fable", "routine", "Jacopo 2026-10-01", NOW - timedelta(days=9))
        usage.record_override(self.home, "proj", "T-2", "gpt-6-astra", "hard", "Jacopo 2026-10-20", NOW - timedelta(days=1))
        self.assertEqual(usage.weekly_lines(self.home, NOW)[-1],
                         "model overrides, last 7 days: 1 (proj/T-2 gpt-6-astra on hard at "
                         f"{(NOW - timedelta(days=1)).isoformat(timespec='seconds')}: Jacopo 2026-10-20)")

    def five(self, vendor, five, age=timedelta(minutes=5), week=50, left=84):
        """Fixture reading with the 5-hour window at `five` percent: claude through its state file, codex through a rollout."""
        if vendor == "claude":
            return self.claude(week, left, five=five / 100, age=age)
        line = json.loads((FIXTURES / "codex-rollout-sample.jsonl").read_text().splitlines()[0])
        line["timestamp"] = (NOW - age).isoformat()
        line["payload"]["rate_limits"] = {
            "primary": {"used_percent": five, "window_minutes": 300, "resets_at": epoch(NOW + FIVE_LEFT)},
            "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": epoch(NOW + timedelta(hours=left))}}
        self.codex(json.dumps(line) + "\n")

    def busy(self, vendor, at=NOW, pid=None):
        usage.claim(self.home, vendor, "lane", os.getpid() if pid is None else pid, at)

    def test_claim_identity_and_pruning_including_legacy_files(self):
        directory = self.home / '.local/state/task/usage/running/claude'
        self.busy('claude')
        path = directory / f'{os.getpid()}.json'
        entry = json.loads(path.read_text())
        fields = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()
        self.assertEqual(entry['start'], fields[19])
        self.assertEqual(usage.running(self.home, 'claude'), 1)
        # The PID is still alive, but its saved identity belongs to another process.
        entry['start'] = str(int(entry['start']) + 1)
        path.write_text(json.dumps(entry))
        self.assertEqual(usage.running(self.home, 'claude'), 0)
        self.assertFalse(path.exists())
        for data in ({'pid': os.getpid(), 'at': NOW.isoformat()},
                     dict(entry, pid=DEAD), {}, dict(entry, pid='invalid')):
            path.write_text(json.dumps(data))
            self.assertEqual(list(usage._claims(self.home, 'claude')), [])
            self.assertFalse(path.exists())
        self.busy('claude', pid=DEAD)
        self.assertEqual(usage.running(self.home, 'claude'), 0)
        self.assertEqual(list(directory.glob('*.json')), [])

    def test_process_start_handles_parentheses_and_dead_processes(self):
        stat = '123 (name with ) parentheses) S ' + ' '.join(['0'] * 18 + ['456'])
        with mock.patch.object(Path, 'read_text', return_value=stat):
            self.assertEqual(usage.process_start(123), '456')
        with mock.patch.object(Path, 'read_text', return_value=stat.replace(') S ', ') Z ')):
            self.assertIsNone(usage.process_start(123))
        self.assertIsNone(usage.process_start(DEAD))

    def test_release_preserves_stale_reading_history(self):
        self.five('claude', 10, age=timedelta(minutes=46))
        self.busy('claude')
        usage.release(self.home, 'claude', os.getpid())
        usage.release(self.home, 'claude', os.getpid())  # idempotent
        self.assertEqual(usage.running(self.home, 'claude'), 0)
        self.assertTrue(self.verdict()['stale'])
        self.assertEqual(usage.gate(self.home, 'lane', 'claude', 'routine', 'p', 't', NOW)['hold'], '')

    def test_five_hour_bands_per_vendor(self):
        # Reserve claude 25 -> exhausted at 75, warning at 60; codex 10 -> 90 and 75.
        for vendor, cases in (("claude", ((59, "ok"), (60, "5h-warning"), (74, "5h-warning"), (75, "5h-exhausted"), (100, "5h-exhausted"))),
                              ("codex", ((74, "ok"), (75, "5h-warning"), (89, "5h-warning"), (90, "5h-exhausted")))):
            for used, expected in cases:
                with self.subTest(vendor=vendor, used=used):
                    self.five(vendor, used)
                    found = self.verdict(vendor)
                    self.assertEqual((found["five"], found["stale"]), (expected, False))
        self.codex()  # the real Codex rollout reports no 5-hour window: ok, never a limit
        self.assertEqual(self.verdict("codex")["five"], "ok")

    def test_five_hour_exhausted_holds_until_the_reset_and_moves_routine_work_to_room(self):
        self.five("claude", 80)
        self.five("codex", 20)
        moved = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertEqual((moved["vendor"], moved["hold"]), ("codex", ""))
        self.assertTrue(any("5-hour window used up; routine lane moves to codex" in note for note in moved["notes"]))
        for kind, tier in (("lane", "hard"), ("review", "routine")):
            held = usage.gate(self.home, kind, "claude", tier, "proj", "t1", NOW)
            self.assertEqual(held["vendor"], "claude")
            self.assertIn("5-hour window used up (80 % of 75 %)", held["hold"])
            self.assertIn(f"until the reset at {NOW + FIVE_LEFT:%Y-%m-%d %H:%M} UTC", held["hold"])
        self.busy("codex")
        self.five("codex", 80)  # codex in its warning band with a lane running: no room there either
        held = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertEqual((held["vendor"], "5-hour window used up" in held["hold"]), ("claude", True))
        self.five("codex", 95)  # codex exhausted as well
        self.assertIn("5-hour window used up", usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)["hold"])
        self.five("claude", 20, week=80)  # weekly exhausted alone still reads as before
        self.assertIn("automated share used up", usage.gate(self.home, "lane", "claude", "hard", "proj", "t1", NOW)["hold"])

    def test_five_hour_warning_allows_one_lane_or_review_machine_wide(self):
        self.five("claude", 65)
        self.five("codex", 30)
        for kind in ("lane", "review"):
            self.assertEqual(usage.gate(self.home, kind, "claude", "routine", "proj", "t1", NOW)["hold"], "")
        self.busy("claude", pid=DEAD)  # a dead pid does not count
        self.assertEqual(usage.running(self.home, "claude"), 0)
        self.busy("claude")
        self.assertEqual(usage.running(self.home, "claude"), 1)
        for kind in ("lane", "review"):
            held = usage.gate(self.home, kind, "claude", "routine", "proj", "t2", NOW)
            self.assertIn("warning band", held["hold"])
            self.assertIn("one at a time", held["hold"])
        self.assertEqual(usage.gate(self.home, "lane", "codex", "routine", "proj", "t3", NOW)["hold"], "")  # other vendor
        self.five("claude", 30)  # below the band, a running lane does not hold the next one
        self.assertEqual(usage.gate(self.home, "lane", "claude", "routine", "proj", "t2", NOW)["hold"], "")

    def test_stale_reading_while_lanes_ran_is_the_warning_band_with_a_warning(self):
        old, fresh = timedelta(minutes=46), timedelta(minutes=44)
        self.five("claude", 10, age=old)
        self.assertEqual(self.verdict()["five"], "ok")  # old, but no lane of this vendor ran
        self.busy("claude", at=NOW - timedelta(hours=1), pid=DEAD)  # ended before the reading: still not stale
        self.assertEqual(self.verdict()["five"], "ok")
        self.busy("claude", at=NOW - timedelta(minutes=30), pid=DEAD)  # ran since the reading, ended
        found = self.verdict()
        self.assertEqual((found["five"], found["stale"]), ("5h-warning", True))
        self.assertTrue(any("claude usage reading is 46 min old while its lanes ran" in note
                            for note in usage.assessments(self.home, NOW)[2]))
        self.assertIn("(reading stale)", "\n".join(usage.report(self.home, NOW)))
        self.busy("claude")  # one running lane: a second one waits on the stale reading
        self.assertIn("(reading stale)", usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)["hold"])
        self.five("claude", 10, age=fresh)
        self.assertEqual(self.verdict()["five"], "ok")  # 44 minutes is fresh enough
        self.assertFalse(any("while its lanes ran" in note for note in usage.assessments(self.home, NOW)[2]))
        self.five("codex", 10, age=old)  # the other vendor has its own lanes
        self.assertEqual(self.verdict("codex")["five"], "ok")
        self.busy("codex")
        self.assertEqual(self.verdict("codex")["five"], "5h-warning")

    def test_no_reading_at_all_is_unknown_and_the_warning_band_for_a_vendor_with_a_5h_window(self):
        found = self.verdict()  # claude always has a 5-hour window; with no reading the weekly pace stays unknown
        self.assertEqual((found["verdict"], found["five"], found["missing"]), ("unknown", "5h-warning", True))
        self.assertEqual(usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)["hold"], "")  # the first one starts
        self.assertEqual(usage.five_caps(self.home, NOW)["claude"], 1)
        self.busy("claude")
        held = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertIn("warning band (no 5-hour reading)", held["hold"])
        self.assertIn("claude: unknown (no rate_limit_event", "\n".join(usage.report(self.home, NOW)))
        self.assertIn("5-hour: 5h-warning (no 5-hour reading)", "\n".join(usage.report(self.home, NOW)))

    def test_an_expired_5h_reading_is_missing_so_the_warning_band_even_when_no_lane_ran(self):
        event = {"rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": 0.1, "resetsAt": epoch(NOW - timedelta(hours=1))},  # that window has reset
            "seven_day": {"utilization": 0.5, "resetsAt": epoch(NOW + timedelta(hours=84))}}}}
        usage.record_claude(self.home, event, NOW - timedelta(hours=3))
        found = self.verdict()
        self.assertEqual((found["five"], found["missing"], found["stale"]), ("5h-warning", True, False))
        self.assertTrue(any("claude has no current 5-hour reading" in note for note in usage.assessments(self.home, NOW)[2]))
        self.assertEqual(usage.five_caps(self.home, NOW)["claude"], 1)
        self.assertIn("(no 5-hour reading)", "\n".join(usage.report(self.home, NOW)))
        self.busy("claude")
        self.assertIn("one at a time", usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)["hold"])
        self.five("claude", 10)  # a fresh reading lifts it
        self.assertEqual(usage.five_caps(self.home, NOW)["claude"], None)

    def test_a_vendor_without_a_5h_window_in_7_days_has_only_the_weekly_rules(self):
        self.codex()  # the real Codex rollout: one weekly window, no 5-hour window
        self.busy("codex")  # the 47-minute-old reading with a running lane is not stale: there is no 5-hour window
        found = self.verdict("codex")
        self.assertEqual((found["has_five"], found["five"], found["missing"], found["stale"]), (False, "ok", False, False))
        self.assertEqual(usage.five_caps(self.home, NOW)["codex"], None)
        text = "\n".join(usage.report(self.home, NOW))
        self.assertIn("5-hour: none; no 5-hour window in the readings of the last 7 days; only the weekly rules apply", text)
        self.assertEqual(usage.gate(self.home, "lane", "codex", "hard", "proj", "t1", NOW)["hold"], "")  # a second lane may start
        line = json.loads((FIXTURES / "codex-rollout-sample.jsonl").read_text().splitlines()[0])
        older = json.loads(json.dumps(line))  # a 5-hour window seen two days ago makes the newest reading's gap a miss
        older["timestamp"] = (NOW - timedelta(days=2)).isoformat()
        older["payload"]["rate_limits"] = {
            "primary": {"used_percent": 5.0, "window_minutes": 300, "resets_at": epoch(NOW - timedelta(days=2) + timedelta(hours=5))},
            "secondary": {"used_percent": 75.0, "window_minutes": 10080, "resets_at": 1793082750}}
        self.codex(json.dumps(older) + "\n" + json.dumps(line) + "\n")
        found = self.verdict("codex")
        self.assertEqual((found["has_five"], found["five"], found["missing"]), (True, "5h-warning", True))
        self.assertEqual(usage.five_caps(self.home, NOW)["codex"], 1)
        older["timestamp"] = (NOW - timedelta(days=8)).isoformat()  # outside the 7 days: no window again
        self.codex(json.dumps(older) + "\n" + json.dumps(line) + "\n")
        self.assertEqual((self.verdict("codex")["has_five"], self.verdict("codex")["five"]), (False, "ok"))

    def test_five_caps_under_the_bands(self):
        self.five("claude", 50)
        self.five("codex", 80)
        self.assertEqual(usage.five_caps(self.home, NOW), {"claude": None, "codex": 1})
        self.five("claude", 80)
        self.five("codex", 95)
        self.assertEqual(usage.five_caps(self.home, NOW), {"claude": 0, "codex": 0})

    def test_usage_shows_five_hour_verdict_while_weekly_line_reads_runs(self):
        event = json.loads((FIXTURES / "claude-5h-event.json").read_text())  # the real 2026-10-21 event: 100 % at 20:10 reset
        at = datetime.fromtimestamp(1792606200, timezone.utc) - timedelta(hours=1)
        usage.record_claude(self.home, event, at)
        text = "\n".join(usage.report(self.home, at))
        self.assertIn("claude: weekly used 38 %, 5-hour used 100 %", text)
        self.assertIn("5-hour: 5h-exhausted, resets at 2026-10-21 18:10 UTC / 2026-10-21 20:10 Europe/Copenhagen", text)
        self.assertIn("no new lanes or reviews on claude until the reset", text)
        self.assertEqual(usage.weekly_lines(self.home, at)[0],
                         "claude last 7d: runs 0, weekly usage delta None, outcomes {}")
        self.assertEqual(usage.weekly_lines(self.home, at + timedelta(minutes=20))[0],
                         usage.weekly_lines(self.home, at)[0])
        self.five("claude", 30, week=38)
        self.assertIn("5-hour: ok", "\n".join(usage.report(self.home, NOW)))

    def test_stream_splits_text_from_events_and_records_the_reading(self):
        stream = usage.ClaudeStream(self.home)
        event = (FIXTURES / "claude-rate-limit-event.json").read_text().strip().encode()
        result = json.dumps({"type": "result", "result": "final words"}).encode()
        text = stream.feed(event + b"\n" + result[:10]) + stream.feed(result[10:] + b"\nplain line")
        self.assertEqual(text + stream.finish(), "final words\nplain line\n")
        state = json.loads((self.home / ".local/state/task/usage/claude.json").read_text())
        self.assertEqual(state["seven_day"]["utilization"], 0.85)
        self.assertEqual(state["five_hour"]["resetsAt"], 1792161600)

class ResetFeed(unittest.TestCase):
    """Decision-24 spend-down from the Codex reset feed: fixtures and a mocked fetch only, never the network."""
    setUp, claude, codex, codex_week, write_policy, verdict, RAMP = (
        Usage.setUp, Usage.claude, Usage.codex, Usage.codex_week, Usage.write_policy, Usage.verdict, Usage.RAMP)

    def feed(self, primary=None, rss=None, now=NOW, events=None):
        """Run one watcher poll; each source is fixture bytes or an exception the mocked fetch raises."""
        if events is not None:
            primary = json.dumps({"events": events, "stats": {}, "generated_at": now.isoformat()}).encode()
        sources = {"primary": (FIXTURES / "codex-resets.json").read_bytes() if primary is None else primary,
                   "rss": (FIXTURES / "codex-reset.xml").read_bytes() if rss is None else rss}
        calls = []

        def fetch(url):
            calls.append(url)
            if isinstance(sources[url], Exception):
                raise sources[url]
            return sources[url]
        state = usage.refresh_resets(self.home, now, ("primary", "rss"), fetch)
        return state, calls

    def lines(self, now=NOW):
        return usage.report(self.home, now)

    def test_feed_is_polled_hourly_and_stored_in_watcher_state(self):
        state, calls = self.feed()
        self.assertEqual((calls, state["ok"], state["source"], len(state["events"])), (["primary"], True, "primary", 4))
        path = self.home / ".local/state/task/codex-resets.json"
        self.assertEqual(json.loads(path.read_text())["events"], state["events"])
        self.assertEqual(self.feed(now=NOW + timedelta(minutes=59))[1], [])  # at most hourly
        self.assertEqual(self.feed(now=NOW + timedelta(hours=1))[1], ["primary"])
        with mock.patch.object(usage.urllib.request, "urlopen", side_effect=AssertionError("network")):
            self.assertEqual(usage.refresh_resets(self.home, NOW, ()), json.loads(path.read_text()))  # feed off: no fetch

    def test_1_announced_reset_ahead_is_codex_spend_down_lifting_ramp_and_ceiling(self):
        self.write_policy(self.RAMP)
        self.claude(50, 84)
        self.codex_week(95, timedelta(hours=2))  # past the day-1 allowance and the 80 % ceiling
        self.assertIn("codex past its daily ramp", usage.gate(self.home, "lane", "codex", "routine", "proj", "t1", NOW)["hold"])
        self.feed()
        target = datetime(2026, 10, 21, 17, tzinfo=timezone.utc)
        self.assertEqual(self.verdict("codex")["spend_down"], target)
        self.assertEqual(usage.spend_down(self.home, "codex", NOW), target)
        decision = usage.gate(self.home, "lane", "codex", "routine", "proj", "t1", NOW)
        self.assertEqual((decision["hold"], decision["ramp_exempt"]), ("", ""))
        self.assertTrue(any(usage.SPEND_DOWN in note for note in decision["notes"]))
        self.assertEqual(usage.ramp_gate(self.home, "red-team", "codex", "proj", "t1", "", NOW), ("", ""))
        text = "\n".join(self.lines())
        self.assertIn("  spend-down until 2026-10-21 17:00 UTC / 2026-10-21 19:00 Europe/Copenhagen: announced reset ahead "
                      "(decision-24 spend-down); ramp and ceiling lifted for codex launches", text)
        self.assertNotIn("past ramp", text)
        self.assertNotIn("codex reset feed", text)
        from task_routing import RoutingParseError, guard
        self.assertTrue(guard("routine", "gpt-6-astra", "", usage.SPEND_DOWN))  # the guard allows it; the caller ledgers
        with self.assertRaises(RoutingParseError):
            guard("routine", "gpt-6-astra", "")

    def test_1_teasers_and_interval_predictions_change_nothing(self):
        self.write_policy(self.RAMP)
        self.codex_week(95, timedelta(hours=2))
        events = json.loads((FIXTURES / "codex-resets.json").read_text())["events"][2:]
        self.feed(events=events)
        self.assertIsNone(self.verdict("codex")["spend_down"])
        self.assertTrue(self.verdict("codex")["ramp"]["over"])
        self.assertIn("past ramp", "\n".join(self.lines()))

    def test_2_completed_or_passed_target_ends_spend_down_and_restarts_the_ramp_at_day_one(self):
        self.write_policy(self.RAMP)
        events = json.loads((FIXTURES / "codex-resets.json").read_text())["events"]
        for name, completed, now in (("completed", "2026-10-20T19:00:00Z", NOW),
                                     ("target passed", None, datetime(2026, 10, 21, 18, tzinfo=timezone.utc))):
            with self.subTest(name):
                self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="usage-test-")))
                self.write_policy(self.RAMP)
                self.codex_week(70, timedelta(days=4), at=datetime(2026, 10, 20, 18, tzinfo=timezone.utc))  # day 5, before the reset
                self.feed(events=[dict(events[0], completed_at=completed)] + events[1:], now=now)
                found = usage.assessments(self.home, now)[1]["codex"]
                self.assertIsNone(found["spend_down"])
                self.assertEqual((found["ramp"]["day"], found["ramp"]["allowance"], found["ramp"]["over"], found["weekly"]),
                                 (1, 20, False, 0.0))
                self.assertIn("  ramp: day 1, allowance 20 % (20 %/day, ceiling 80 %); within ramp", self.lines(now))
                self.assertFalse(any("spend-down until" in line for line in self.lines(now)))

    def test_2_a_landed_reset_after_the_announcement_ends_spend_down(self):
        self.write_policy(self.RAMP)
        self.codex_week(95, timedelta(hours=2))
        events = json.loads((FIXTURES / "codex-resets.json").read_text())["events"]
        self.feed(events=events + [{"announced_at": None, "target_date": None, "completed_at": "2026-10-20T19:30:00Z",
                                    "event_status": "confirmed", "tweet_url": "https://x.com/example/status/4"}])
        self.assertIsNone(self.verdict("codex")["spend_down"])
        self.assertTrue(self.verdict("codex")["ramp"]["over"])  # the reading at 19:55 is already after the reset

    def test_3_primary_failing_uses_the_rss_fallback(self):
        self.write_policy(self.RAMP)
        self.codex_week(70, timedelta(days=4), at=datetime(2026, 10, 20, 18, tzinfo=timezone.utc))
        for primary in (OSError("connection refused"), b"<html>not json</html>", b'{"events": "nope"}'):
            with self.subTest(primary=primary):
                (self.home / ".local/state/task/codex-resets.json").unlink(missing_ok=True)
                state, calls = self.feed(primary=primary)
                self.assertEqual((calls, state["ok"], state["source"]), (["primary", "rss"], True, "rss"))
                self.assertEqual(sorted(state["events"]), ["reset-2026-10-13", "reset-2026-10-20"])
                found = self.verdict("codex")
                self.assertIsNone(found["spend_down"])  # the RSS feed has only landed resets
                self.assertEqual(found["ramp"]["day"], 1)  # its 19:00 reset landed after the 18:00 reading
                self.assertFalse(any("codex reset feed" in line for line in self.lines()))

    def test_3_both_failing_means_no_spend_down_and_one_warning_line(self):
        self.write_policy(self.RAMP)
        self.claude(50, 84)
        self.codex_week(95, timedelta(hours=2))
        self.feed()  # an announced reset is stored, then the feed goes down
        later = NOW + timedelta(hours=1)
        state, calls = self.feed(primary=OSError("down"), rss=b"<rss><channel><item><title>x</title></item>", now=later)
        self.assertEqual((calls, state["ok"]), (["primary", "rss"], False))
        found = usage.assessments(self.home, later)[1]["codex"]
        self.assertIsNone(found["spend_down"])
        self.assertTrue(found["ramp"]["over"])
        self.assertIn("codex past its daily ramp", usage.gate(self.home, "lane", "codex", "routine", "proj", "t1", later)["hold"])
        warnings = [line for line in self.lines(later) if "codex reset feed" in line]
        self.assertEqual(warnings, ["pace warning: codex reset feed down or unparseable; no spend-down, the codex ramp applies"])
        stale = '{"ok": true, "checked_at": "2026-10-20T15:00:00+00:00", "events": {}}'
        for state_text, expected in (("{broken", "has no reading yet"), (stale, "not checked since 2026-10-20 15:00 UTC")):
            (self.home / ".local/state/task/codex-resets.json").write_text(state_text)
            self.assertEqual([line for line in self.lines(later) if "codex reset feed" in line],
                             [f"pace warning: codex reset feed {expected}; no spend-down, the codex ramp applies"])

    def test_4_claude_is_unaffected_in_every_case(self):
        self.write_policy(self.RAMP)
        self.claude(80, 84)  # exhausted: Claude stays held in every feed state
        before = usage.gate(self.home, "lane", "claude", "hard", "proj", "t1", NOW)["hold"]
        for primary in (None, OSError("down")):
            with self.subTest(primary=primary):
                (self.home / ".local/state/task/codex-resets.json").unlink(missing_ok=True)
                self.feed(primary=primary, rss=OSError("down"))
                self.assertIsNone(self.verdict("claude")["spend_down"])
                self.assertIsNone(usage.spend_down(self.home, "claude", NOW))
                self.assertEqual(usage.gate(self.home, "lane", "claude", "hard", "proj", "t1", NOW)["hold"], before)
                self.assertFalse(any("spend-down" in line for line in self.lines() if line.startswith("claude")))


if __name__ == "__main__":
    unittest.main()
