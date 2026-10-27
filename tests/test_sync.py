import gzip
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sync

RAW = {
    "id": "b733ffe0",
    "title": "Strength B — Upper + Trunk (Tuesday)",
    "start_time": "2026-10-27T07:08:42+00:00",
    "end_time": "2026-10-27T07:42:58+00:00",
    "exercises": [
        {"title": "Bench Press (Barbell)", "sets": [
            {"type": "warmup", "weight_kg": 32, "reps": 8},
            {"type": "normal", "weight_kg": 32, "reps": 8},
        ]},
        {"title": "Bent Over Row (Barbell)", "sets": [
            {"type": "normal", "weight_kg": 42, "reps": 8},
        ]},
        {"title": "Hollow-Body Hold", "sets": [
            {"type": "normal", "duration_seconds": 45},
        ]},
    ],
}


def gset(cat, name, weight=0.0, reps=None, set_type="ACTIVE"):
    return {"setType": set_type, "weight": weight, "repetitionCount": reps,
            "exercises": [{"category": cat, "name": name}]}


def workout():
    return sync.parse_hevy_workout(RAW)


def test_volume_counts_every_weighted_set():
    assert workout().volume_kg == 32 * 8 + 32 * 8 + 42 * 8


def test_describe_lists_sets_and_volume():
    text = sync.describe(workout())
    assert text.startswith(sync.DESCRIPTION_MARKER)
    assert "Bench Press (Barbell): 32kg×8 (w), 32kg×8" in text
    assert "Hollow-Body Hold: 45s" in text
    assert text.endswith("Volume: 848 kg")


def test_closest_by_start_respects_window():
    t = workout().start
    near = {"t": t + timedelta(minutes=1)}
    far = {"t": t + timedelta(hours=2)}
    assert sync.closest_by_start([far, near], lambda c: c["t"], t) is near
    assert sync.closest_by_start([far], lambda c: c["t"], t) is None


def test_garmin_start_is_utc():
    assert sync.garmin_start({"startTimeGMT": "2026-10-27 07:08:38"}) == datetime(
        2026, 10, 27, 7, 8, 38, tzinfo=timezone.utc)


def test_align_and_write_grams():
    sets = [
        gset("BENCH_PRESS", None, reps=10),
        gset(None, None, set_type="REST"),
        gset("BENCH_PRESS", None, reps=8),
        gset("ROW", None, reps=8),
        gset("CORE", None, reps=1),
    ]
    pairs, note = sync.align_sets(sets, workout())
    assert note == "4 of 4 sets"
    assert sync.apply_hevy_sets(pairs) == 3  # the hold has no weight or reps to change
    assert [s["weight"] for s in sets if s["setType"] == "ACTIVE"] == [32000.0, 32000.0, 42000.0, 0.0]
    assert sets[0]["repetitionCount"] == 8
    assert sync.apply_hevy_sets(pairs) == 0  # second run changes nothing


def hevy(*exercises):
    return sync.HevyWorkout("w", "t", datetime.now(timezone.utc), datetime.now(timezone.utc), [
        sync.HevyExercise(title, [sync.HevySet(kg, r) for kg, r in sets]) for title, sets in exercises
    ])


def test_one_sided_exercise_fills_left_and_right():
    w = hevy(("Squat (Barbell)", [(72, 6)] * 2),
             ("Bulgarian Split Squat (Dumbbell)", [(48, 8)] * 2),
             ("Romanian Deadlift (Trap Bar)", [(81, 8)]))
    sets = [gset("UNKNOWN", None, reps=r) for r in (6, 6, 8, 8, 8, 8, 8)]
    pairs, note = sync.align_sets(sets, w)
    assert "left + right for Bulgarian Split Squat (Dumbbell)" in note
    sync.apply_hevy_sets(pairs)
    assert [s["weight"] / 1000 for s in sets] == [72, 72, 48, 48, 48, 48, 81]


def test_extra_garmin_set_is_skipped_and_sides_inferred_from_reps():
    # As on 20 Oct: an extra set before the bench press, and a kettlebell press
    # whose name does not say it is one-sided but whose reps show left + right.
    w = hevy(("Bench Press (Barbell)", [(42, 8), (42, 10), (42, 10)]),
             ("Kettlebell Shoulder Press", [(14, 10)] * 2),
             ("Ball Slams", [(12, 6), (12, 7)]))
    reps = (10, 8, 10, 10, 10, 10, 10, 10, 6, 7)
    sets = [gset("UNKNOWN", None, reps=r) for r in reps]
    pairs, note = sync.align_sets(sets, w)
    assert "1 extra Garmin set(s) left alone" in note
    assert "left + right for Kettlebell Shoulder Press" in note
    sync.apply_hevy_sets(pairs)
    assert [s["weight"] / 1000 for s in sets] == [0, 42, 42, 42, 14, 14, 14, 14, 12, 12]


def test_align_gives_up_when_nothing_lines_up():
    pairs, note = sync.align_sets([gset("UNKNOWN", None)], workout())
    assert pairs == [] and note.startswith("left the sets alone")


def test_intervals_changes_keeps_hand_written_description():
    w = workout()
    act = {"name": "Strength B — Upper + Trunk", "kg_lifted": None, "description": "felt strong"}
    assert sync.intervals_changes(act, w) == {"name": w.title, "kg_lifted": 848}


def test_intervals_changes_fills_empty_and_is_idempotent():
    w = workout()
    changes = sync.intervals_changes({"name": "x", "description": None}, w)
    assert changes["description"] == sync.describe(w)
    done = {"name": w.title, "kg_lifted": w.volume_kg, "description": sync.describe(w)}
    assert sync.intervals_changes(done, w) == {}


def test_describe_token_file_never_prints_values(tmp_path):
    f = tmp_path / "garmin_tokens.json"
    f.write_text('{"oauth1_token": "SECRET", "oauth2_token": "SECRET"}')
    msg = sync.describe_token_file(f)
    assert "SECRET" not in msg and "missing" in msg
    f.write_text("not json SECRET")
    assert "SECRET" not in sync.describe_token_file(f)


def test_normalise_decodes_base64_tokens(tmp_path):
    import base64
    import json
    f = tmp_path / "garmin_tokens.json"
    data = {"di_token": "a", "di_refresh_token": "b", "di_client_id": "c"}
    f.write_text(base64.b64encode(json.dumps(data).encode()).decode())
    sync.normalise_token_file(f)
    assert json.loads(f.read_text()) == data


def test_load_env_file_keeps_existing_values(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('# comment\nexport HEVY_API_KEY="abc"\nINTERVALS_API_KEY=xyz\nLOOKBACK_DAYS=5\n')
    monkeypatch.delenv("HEVY_API_KEY", raising=False)
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    monkeypatch.setenv("LOOKBACK_DAYS", "2")
    sync.load_env_file(f)
    import os
    assert os.environ["HEVY_API_KEY"] == "abc"
    assert os.environ["INTERVALS_API_KEY"] == "xyz"
    assert os.environ["LOOKBACK_DAYS"] == "2"


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def hevy_page(starts, page, page_count):
    return FakeResponse({"page": page, "page_count": page_count, "workouts": [
        dict(RAW, id=s, start_time=s, end_time=s) for s in starts
    ]})


def test_fetch_all_walks_every_page(monkeypatch):
    pages = {
        1: hevy_page(["2026-10-27T07:00:00+00:00", "2026-10-25T07:00:00+00:00"], 1, 2),
        2: hevy_page(["2024-01-02T07:00:00+00:00"], 2, 2),
    }
    monkeypatch.setattr(sync.requests, "get", lambda *a, params, **k: pages[params["page"]])
    every = sync.fetch_hevy_workouts("key", None)
    assert [w.id[:4] for w in every] == ["2024", "2026", "2026"]  # oldest first
    recent = sync.fetch_hevy_workouts("key", datetime(2026, 10, 26, tzinfo=timezone.utc))
    assert [w.id[:10] for w in recent] == ["2026-10-27"]


class FakeGarmin:
    def __init__(self, activities):
        self.activities = activities
        self.ranges, self.renamed = [], {}

    def get_activities_by_date(self, start, end):
        self.ranges.append((start, end))
        return self.activities

    def set_activity_name(self, aid, name):
        self.renamed[aid] = name

    def get_activity_exercise_sets(self, aid):
        return {"exerciseSets": []}


class FakeIntervals:
    def __init__(self, acts, fit=b"", times=(), current=()):
        self.acts, self.ranges, self.updates = acts, [], {}
        self.fit, self.times, self.current = fit, list(times), list(current)
        self.written = {}

    def activities(self, oldest, newest):
        self.ranges.append((oldest, newest))
        return self.acts

    def update(self, aid, changes):
        self.updates[aid] = changes

    def original_file(self, aid):
        return self.fit

    def time_stream(self, aid):
        return self.times

    def intervals(self, aid):
        return self.current

    def set_intervals(self, aid, intervals):
        self.written[aid] = intervals


def test_backfill_lists_each_side_once_and_matches_every_workout(monkeypatch):
    w_old = sync.parse_hevy_workout(dict(RAW, id="old", start_time="2025-03-01T18:00:00+00:00",
                                         end_time="2025-03-01T19:00:00+00:00"))
    w_new = workout()
    garmin = FakeGarmin([
        {"activityId": 1, "activityName": "Strength", "startTimeGMT": "2025-03-01 18:01:00",
         "activityType": {"typeKey": "strength_training"}},
        {"activityId": 2, "activityName": "Strength B", "startTimeGMT": "2026-10-27 07:08:38",
         "activityType": {"typeKey": "strength_training"}},
    ])
    icu = FakeIntervals([
        {"id": "i1", "type": "WeightTraining", "external_id": "1", "start_date": "2025-03-01T18:01:00Z"},
        {"id": "i2", "type": "WeightTraining", "external_id": "2", "start_date": "2026-10-27T07:08:38Z"},
    ])
    monkeypatch.setattr(sync, "fetch_hevy_workouts", lambda key, since: [w_old, w_new])
    monkeypatch.setattr(sync, "garmin_login", lambda store: garmin)
    monkeypatch.setattr(sync, "Intervals", lambda key, athlete: icu)
    monkeypatch.setattr(sync, "BACKFILL_PAUSE", 0)
    monkeypatch.setenv("HEVY_API_KEY", "k")
    monkeypatch.setenv("INTERVALS_API_KEY", "k")
    monkeypatch.setenv("GARMIN_PASSWORD", "p")

    assert sync.main(["--all"]) == 0
    assert garmin.ranges == [("2025-02-28", "2026-10-28")]
    assert icu.ranges == [("2025-02-28", "2026-10-28")]
    assert garmin.renamed == {1: w_old.title, 2: w_new.title}
    assert set(icu.updates) == {"i1", "i2"}


def one_workout_on_both_sides(monkeypatch):
    garmin = FakeGarmin([
        {"activityId": 2, "activityName": "Strength B", "startTimeGMT": "2026-10-27 07:08:38",
         "activityType": {"typeKey": "strength_training"}},
    ])
    icu = FakeIntervals([
        {"id": "i2", "type": "WeightTraining", "external_id": "2", "start_date": "2026-10-27T07:08:38Z"},
    ])
    monkeypatch.setattr(sync, "fetch_hevy_workouts", lambda key, since: [workout()])
    monkeypatch.setattr(sync, "garmin_login", lambda store: garmin)
    monkeypatch.setattr(sync, "Intervals", lambda key, athlete: icu)
    monkeypatch.setenv("HEVY_API_KEY", "k")
    monkeypatch.setenv("INTERVALS_API_KEY", "k")
    monkeypatch.setenv("GARMIN_PASSWORD", "p")
    monkeypatch.delenv("SYNC_TARGET", raising=False)
    return garmin, icu


def test_target_garmin_leaves_intervals_alone(monkeypatch):
    garmin, icu = one_workout_on_both_sides(monkeypatch)
    assert sync.main(["--target", "garmin"]) == 0
    assert garmin.renamed == {2: workout().title}
    assert icu.ranges == [] and icu.updates == {}


def test_target_intervals_from_env_leaves_garmin_alone(monkeypatch):
    garmin, icu = one_workout_on_both_sides(monkeypatch)
    monkeypatch.setenv("SYNC_TARGET", "intervals")
    assert sync.main([]) == 0
    assert garmin.ranges == [] and garmin.renamed == {}
    assert set(icu.updates) == {"i2"}  # matched by start time, without Garmin


def test_chosen_target_without_credentials_fails(monkeypatch):
    garmin, icu = one_workout_on_both_sides(monkeypatch)
    monkeypatch.delenv("INTERVALS_API_KEY")
    assert sync.main(["--target", "intervals"]) == 1
    monkeypatch.setenv("GARMINTOKENS", "/nonexistent")
    monkeypatch.delenv("GARMIN_PASSWORD")
    assert sync.main(["--target", "both"]) == 0  # "both" skips a missing side quietly


# --------------------------------------------------------------------------- #
# FIT sets -> Intervals.icu intervals
#
# tests/fixtures/strength.fit is a Garmin-style recording of the RAW workout
# (made by tests/fixtures/make_strength_fit.py): 320 one-second records from
# 07:08:38 UTC and seven `set` messages, four active and three rests.
# --------------------------------------------------------------------------- #

FIT = (Path(__file__).parent / "fixtures" / "strength.fit").read_bytes()
FIT_START = datetime(2026, 10, 27, 7, 8, 38, tzinfo=timezone.utc)
ICU_ACT = {"id": "i9", "type": "WeightTraining", "name": RAW["title"], "kg_lifted": 848,
           "start_date": "2026-10-27T07:08:38Z"}
EXPECTED_INTERVALS = [
    {"start_index": 5, "end_index": 35, "type": "WORK", "label": "Bench Press (Barbell): 32kg×8 (w)"},
    {"start_index": 35, "end_index": 95, "type": "RECOVERY", "label": "Rest"},
    {"start_index": 95, "end_index": 125, "type": "WORK", "label": "Bench Press (Barbell): 32kg×8"},
    {"start_index": 125, "end_index": 185, "type": "RECOVERY", "label": "Rest"},
    {"start_index": 185, "end_index": 215, "type": "WORK", "label": "Bent Over Row (Barbell): 42kg×8"},
    {"start_index": 215, "end_index": 260, "type": "RECOVERY", "label": "Rest"},
    {"start_index": 260, "end_index": 305, "type": "WORK", "label": "Hollow-Body Hold: 45s"},
]


def test_fit_sets_reads_every_set_message():
    sets = sync.fit_sets(FIT)
    assert [s["setType"] for s in sets] == ["ACTIVE", "REST"] * 3 + ["ACTIVE"]
    assert [s["repetitionCount"] for s in sets if s["setType"] == "ACTIVE"] == [8, 8, 8, 0]
    assert [s["exercises"][0]["category"] for s in sets if s["setType"] == "ACTIVE"] == [
        "BENCH_PRESS", "BENCH_PRESS", "ROW", "CRUNCH"]
    assert sets[0]["start"] == FIT_START + timedelta(seconds=5)
    assert [s["duration"] for s in sets] == [30, 60, 30, 60, 30, 45, 45]


def test_fit_sets_ignores_files_that_are_not_fit():
    assert sync.fit_sets(b"<?xml version='1.0'?><gpx/>") == []


def test_build_intervals_labels_each_set_from_hevy():
    w = workout()
    sets = sync.fit_sets(FIT)
    pairs, _ = sync.align_sets(sets, w)
    got = sync.build_intervals(sets, pairs, w, FIT_START, list(range(320)))
    assert got == EXPECTED_INTERVALS


def test_build_intervals_follows_the_time_stream_and_marks_extra_sets():
    w = workout()
    t0 = FIT_START
    sets = [
        {"setType": "ACTIVE", "repetitionCount": 8, "start": t0 + timedelta(seconds=10), "duration": 20},
        {"setType": "ACTIVE", "repetitionCount": 3, "start": t0 + timedelta(seconds=40), "duration": 10},
        {"setType": "REST", "repetitionCount": None, "start": t0 + timedelta(seconds=50.5), "duration": 0.5},
        {"setType": "ACTIVE", "repetitionCount": 8, "start": t0 + timedelta(seconds=900), "duration": 30},
    ]
    pairs = [(sets[0], w.exercises[0].sets[1])]
    times = list(range(0, 60, 2))  # one sample every 2 s, so index = seconds / 2
    got = sync.build_intervals(sets, pairs, w, t0, times)
    assert got == [
        {"start_index": 5, "end_index": 15, "type": "WORK", "label": "Bench Press (Barbell): 32kg×8"},
        {"start_index": 20, "end_index": 25, "type": "WORK", "label": "Extra set (not in Hevy)"},
    ]  # the half-second rest and the set after the recording ended are left out


def test_build_intervals_never_overlap():
    w = workout()
    t0 = FIT_START
    sets = [  # the watch rounds: the rest starts 0.6 s before the set ends
        {"setType": "ACTIVE", "repetitionCount": 8, "start": t0, "duration": 23.6},
        {"setType": "REST", "repetitionCount": None, "start": t0 + timedelta(seconds=23), "duration": 19},
    ]
    got = sync.build_intervals(sets, [(sets[0], w.exercises[0].sets[1])], w, t0, list(range(60)))
    assert [(i["start_index"], i["end_index"]) for i in got] == [(0, 24), (24, 42)]


def test_sync_intervals_writes_intervals_then_leaves_them_alone():
    icu = FakeIntervals([ICU_ACT], fit=FIT, times=range(320))
    assert sync.sync_intervals(icu, workout(), None, icu.acts, dry_run=False)
    assert icu.written == {"i9": EXPECTED_INTERVALS}

    # What Intervals.icu hands back: every interval typed WORK, and an unlabelled
    # filler over the end of the recording that no set covers.
    stored = [dict(i, type="WORK") for i in EXPECTED_INTERVALS] + [
        {"start_index": 305, "end_index": 320, "type": "RECOVERY", "label": None}]
    again = FakeIntervals([ICU_ACT], fit=FIT, times=range(320), current=stored)
    sync.sync_intervals(again, workout(), None, again.acts, dry_run=False)
    assert again.written == {}

    moved = [dict(i) for i in stored]
    moved[0]["label"] = "Something else"
    changed = FakeIntervals([ICU_ACT], fit=FIT, times=range(320), current=moved)
    sync.sync_intervals(changed, workout(), None, changed.acts, dry_run=False)
    assert changed.written == {"i9": EXPECTED_INTERVALS}


def test_sync_intervals_dry_run_writes_nothing():
    icu = FakeIntervals([ICU_ACT], fit=FIT, times=range(320))
    sync.sync_intervals(icu, workout(), None, icu.acts, dry_run=True)
    assert icu.written == {} and icu.updates == {}


def test_sync_intervals_leaves_intervals_alone_when_sets_do_not_line_up():
    more = dict(RAW, exercises=RAW["exercises"] + [  # Hevy now has 7 sets, the FIT only 4
        {"title": "Pull Up", "sets": [{"type": "normal", "reps": 6}] * 3}])
    icu = FakeIntervals([ICU_ACT], fit=FIT, times=range(320))
    sync.sync_intervals(icu, sync.parse_hevy_workout(more), None, icu.acts, dry_run=False)
    assert icu.written == {}


def test_original_file_unzips_gzip():
    class Resp:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    icu = sync.Intervals("k")
    icu.s.get = lambda url, timeout: Resp(gzip.compress(FIT))
    assert icu.original_file("i9") == FIT
    icu.s.get = lambda url, timeout: Resp(FIT)
    assert icu.original_file("i9") == FIT
