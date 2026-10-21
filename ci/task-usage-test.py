#!/usr/bin/env python3
"""Usage readers, policy, pace verdicts and pace gate, on fixture readings in a temporary HOME.

Never reads the real ~/.codex, ~/.claude or ~/.local/state: every path comes from the temporary HOME.
"""
import json
import shutil
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "home/dot_local/bin"))
sys.dont_write_bytecode = True
import task_usage as usage  # noqa: E402

FIXTURES = ROOT / "ci/fixtures/usage"
NOW = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)  # a minute-level hour after the Codex fixture


def epoch(when):
    return int(when.timestamp())


class Usage(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="usage-test-")))

    def claude(self, used, left, five=0.1, age=timedelta(minutes=5)):
        """Write the state a recorded stream leaves: used is a percent, left hours until the weekly reset."""
        reset = NOW + timedelta(hours=left)
        event = {"rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": five, "resetsAt": epoch(NOW + timedelta(hours=2))},
            "seven_day": {"utilization": used / 100, "resetsAt": epoch(reset)}}}}
        usage.record_claude(self.home, event, NOW - age)

    def codex(self, text=None, timestamp=None):
        path = self.home / ".codex/sessions/2026/10/20/rollout-2026-10-20T19-00-00-abc.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text is not None else (FIXTURES / "codex-rollout-sample.jsonl").read_text())

    def verdict(self, vendor="claude", now=NOW):
        pol = usage.policy(self.home)
        return usage.assess(usage.readings(self.home, now)[vendor], pol, now)

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
        other.write_text("reserve = 5\n")
        with self.assertRaises(ValueError):
            usage.policy(self.home)
        decision = usage.gate(self.home, "lane", "claude", "routine", "proj", "t1", NOW)
        self.assertTrue(any("built-in defaults used" in note for note in decision["notes"]))
        other.write_text("bogus = 1\n")
        with self.assertRaises(ValueError):
            usage.policy(self.home)

    def test_example_policy_file_parses_and_matches_the_defaults(self):
        example = tomllib.loads((ROOT / "docs/usage-pacing.example.toml").read_text())
        pointer = self.home / ".config/task/usage-policy-path"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(str(ROOT / "docs/usage-pacing.example.toml"))
        found = usage.policy(self.home)
        self.assertEqual({key: found[key] for key in example}, example)
        self.assertEqual({key: value for key, value in found.items() if key != "research_backlog"},
                         {key: value for key, value in usage.DEFAULTS.items() if key != "research_backlog"})

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

    def test_weekly_line_counts_held_back_lanes_and_the_exhaustion_day(self):
        self.claude(80, 84)
        usage.gate(self.home, "lane", "claude", "hard", "proj", "t1", NOW)
        usage.gate(self.home, "lane", "claude", "hard", "proj", "t1", NOW)  # same lane again: counted once
        usage.gate(self.home, "review", "claude", "routine", "proj", "t2", NOW)
        lines = usage.weekly_lines(self.home, NOW)
        self.assertEqual(lines[0], "claude week ending 2026-10-24: used 80 %, exhausted Tue 2026-10-20, lanes held back 2")
        self.assertEqual(lines[1], "codex week ending unknown: used unknown, exhausted no, lanes held back 0")
        self.claude(30, 20)  # the next reading, in a later week: its own counters
        later = usage.weekly_lines(self.home, NOW)[0]
        self.assertEqual(later, "claude week ending 2026-10-21: used 30 %, exhausted no, lanes held back 0")

    def test_weekly_line_lists_this_weeks_model_overrides_only(self):
        self.assertEqual(usage.weekly_lines(self.home, NOW)[-1], "model overrides, last 7 days: 0")
        usage.record_override(self.home, "proj", "T-1", "fable", "routine", "Jacopo 2026-10-01", NOW - timedelta(days=9))
        usage.record_override(self.home, "proj", "T-2", "gpt-6-astra", "hard", "Jacopo 2026-10-20", NOW - timedelta(days=1))
        self.assertEqual(usage.weekly_lines(self.home, NOW)[-1],
                         "model overrides, last 7 days: 1 (proj/T-2 gpt-6-astra on hard at "
                         f"{(NOW - timedelta(days=1)).isoformat(timespec='seconds')}: Jacopo 2026-10-20)")

    def test_stream_splits_text_from_events_and_records_the_reading(self):
        stream = usage.ClaudeStream(self.home)
        event = (FIXTURES / "claude-rate-limit-event.json").read_text().strip().encode()
        result = json.dumps({"type": "result", "result": "final words"}).encode()
        text = stream.feed(event + b"\n" + result[:10]) + stream.feed(result[10:] + b"\nplain line")
        self.assertEqual(text + stream.finish(), "final words\nplain line\n")
        state = json.loads((self.home / ".local/state/task/usage/claude.json").read_text())
        self.assertEqual(state["seven_day"]["utilization"], 0.85)
        self.assertEqual(state["five_hour"]["resetsAt"], 1792161600)


if __name__ == "__main__":
    unittest.main()
