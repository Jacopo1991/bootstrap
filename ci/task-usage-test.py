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

    def five(self, vendor, five, age=timedelta(minutes=5), week=50):
        """Fixture reading with the 5-hour window at `five` percent: claude through its state file, codex through a rollout."""
        if vendor == "claude":
            return self.claude(week, 84, five=five / 100, age=age)
        line = json.loads((FIXTURES / "codex-rollout-sample.jsonl").read_text().splitlines()[0])
        line["timestamp"] = (NOW - age).isoformat()
        line["payload"]["rate_limits"] = {
            "primary": {"used_percent": five, "window_minutes": 300, "resets_at": epoch(NOW + FIVE_LEFT)},
            "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": epoch(NOW + timedelta(hours=84))}}
        self.codex(json.dumps(line) + "\n")

    def busy(self, vendor, at=NOW, pid=None):
        usage.claim(self.home, vendor, "lane", os.getpid() if pid is None else pid, at)

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


if __name__ == "__main__":
    unittest.main()
