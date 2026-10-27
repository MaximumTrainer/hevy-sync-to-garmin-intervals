from datetime import datetime, timedelta, timezone

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
