"""Copy Hevy workout details onto the matching Garmin Connect and Intervals.icu activities.

For each Hevy workout in the lookback window:

* Garmin Connect: the strength activity that started within MATCH_WINDOW of the
  Hevy workout gets Hevy's title (the watch truncates long workout names) and
  the weight and reps of every set Hevy recorded.
* Intervals.icu: the activity imported from that Garmin activity gets the same
  title, ``kg_lifted`` set to the Hevy volume, and a set-by-set breakdown in
  its description. A description someone wrote by hand is never overwritten.
  The ``set`` messages in the activity's FIT file (one per set, active or rest,
  each with its start time and duration) become the activity's intervals,
  labelled with Hevy's exercise, weight and reps, so the timeline shows heart
  rate per exercise.

Running it twice changes nothing the second time, so it is safe to re-run.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("hevy-sync")

HEVY_API = "https://api.hevyapp.com/v1"
INTERVALS_API = "https://intervals.icu/api/v1"
MATCH_WINDOW = timedelta(minutes=20)
TARGETS = ("both", "garmin", "intervals")  # choices for --target / SYNC_TARGET
BACKFILL_THRESHOLD = 10   # more workouts than this in one run counts as a backfill
BACKFILL_PAUSE = 1.0      # seconds between workouts on a backfill
DESCRIPTION_MARKER = "— synced from Hevy —"
GARMIN_STRENGTH_TYPES = {"strength_training", "indoor_cardio", "hiit"}
INTERVALS_STRENGTH_TYPES = {"WeightTraining", "Workout", "HighIntensityIntervalTraining"}


# --------------------------------------------------------------------------- #
# Hevy
# --------------------------------------------------------------------------- #


@dataclass
class HevySet:
    weight_kg: float | None
    reps: int | None
    set_type: str = "normal"
    duration_seconds: float | None = None


@dataclass
class HevyExercise:
    title: str
    sets: list[HevySet] = field(default_factory=list)


@dataclass
class HevyWorkout:
    id: str
    title: str
    start: datetime
    end: datetime
    exercises: list[HevyExercise]

    @property
    def volume_kg(self) -> float:
        """Total weight × reps across every set, warm-ups included, as Hevy shows it."""
        return round(
            sum((s.weight_kg or 0) * (s.reps or 0) for e in self.exercises for s in e.sets),
            1,
        )


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_hevy_workout(raw: dict[str, Any]) -> HevyWorkout:
    return HevyWorkout(
        id=raw["id"],
        title=raw["title"].strip(),
        start=parse_time(raw["start_time"]),
        end=parse_time(raw["end_time"]),
        exercises=[
            HevyExercise(
                title=e["title"],
                sets=[
                    HevySet(
                        weight_kg=s.get("weight_kg"),
                        reps=s.get("reps"),
                        set_type=s.get("type") or "normal",
                        duration_seconds=s.get("duration_seconds"),
                    )
                    for s in e.get("sets") or []
                ],
            )
            for e in raw.get("exercises") or []
        ],
    )


def fetch_hevy_workouts(api_key: str, since: datetime | None) -> list[HevyWorkout]:
    """Workouts that started at or after `since` (or every workout, if None), oldest first."""
    workouts: list[HevyWorkout] = []
    page = 1
    while True:
        r = requests.get(
            f"{HEVY_API}/workouts",
            headers={"api-key": api_key, "accept": "application/json"},
            params={"page": page, "pageSize": 10},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        batch = [parse_hevy_workout(w) for w in body.get("workouts", [])]
        workouts.extend(w for w in batch if since is None or w.start >= since)
        # Hevy lists newest first, so once a page reaches past `since` we are done.
        past_since = since is not None and batch and batch[-1].start < since
        if not batch or past_since or page >= body.get("page_count", page):
            break
        page += 1
    return sorted(workouts, key=lambda w: w.start)


# --------------------------------------------------------------------------- #
# Matching and text
# --------------------------------------------------------------------------- #


def closest_by_start(
    candidates: list[dict[str, Any]], start_of, target: datetime
) -> dict[str, Any] | None:
    """The candidate whose start is nearest `target`, if within MATCH_WINDOW."""
    best, best_gap = None, MATCH_WINDOW
    for c in candidates:
        try:
            gap = abs(start_of(c) - target)
        except (KeyError, TypeError, ValueError):
            continue
        if gap <= best_gap:
            best, best_gap = c, gap
    return best


def fmt_num(x: float) -> str:
    return f"{x:g}"


def fmt_set(s: HevySet) -> str:
    """'42kg×10', '12 reps' or '45s', with ' (w)' for a warm-up; '' if there is nothing to show."""
    if s.weight_kg and s.reps:
        p = f"{fmt_num(s.weight_kg)}kg×{s.reps}"
    elif s.reps:
        p = f"{s.reps} reps"
    elif s.duration_seconds:
        p = f"{fmt_num(s.duration_seconds)}s"
    else:
        return ""
    return p + (" (w)" if s.set_type == "warmup" else "")


def describe(workout: HevyWorkout) -> str:
    lines = [DESCRIPTION_MARKER]
    for ex in workout.exercises:
        parts = [p for p in map(fmt_set, ex.sets) if p]
        lines.append(f"{ex.title}: {', '.join(parts)}" if parts else ex.title)
    lines.append(f"Volume: {fmt_num(workout.volume_kg)} kg")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Garmin
# --------------------------------------------------------------------------- #


def garmin_start(activity: dict[str, Any]) -> datetime:
    return datetime.strptime(activity["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )


UNILATERAL_WORDS = ("single", "one-arm", "one arm", "one-leg", "one leg", "unilateral",
                    "bulgarian", "split squat", "lunge", "step up", "step-up", "alternat")
SKIP_COST = 3.0         # a Garmin set with no Hevy counterpart (an extra the watch detected)
PER_SIDE_COST = 1.0     # per set, for treating an exercise not named as one-sided as left + right
MAX_SKIPPED = 3


def looks_unilateral(title: str) -> bool:
    t = title.lower()
    return any(w in t for w in UNILATERAL_WORDS)


def _rep_cost(h: HevySet, g: dict[str, Any]) -> float:
    if not h.reps:  # timed or distance sets: Garmin's rep count means nothing here
        return 0.0
    return min(abs((g.get("repetitionCount") or 0) - h.reps), 5)


def align_sets(
    garmin_sets: list[dict[str, Any]], workout: HevyWorkout
) -> tuple[list[tuple[dict[str, Any], HevySet]], str]:
    """Pair Garmin ACTIVE sets with Hevy sets.

    Hevy logs a one-sided exercise as one set covering both sides; the watch
    records a set per side. The watch also picks up the odd set Hevy never saw.
    So each Hevy exercise takes either one Garmin set per Hevy set or two (left
    and right), and Garmin sets may be skipped; the cheapest arrangement by rep
    count wins. Returns the pairs and a note on what was done, or why nothing
    was paired.
    """
    active = [s for s in garmin_sets if s.get("setType") == "ACTIVE"]
    exercises = [e for e in workout.exercises if e.sets]
    n_g, n_e = len(active), len(exercises)
    inf = float("inf")
    # best[i][j]: cheapest way to place exercises i.. on Garmin sets j..
    best = [[inf] * (n_g + 1) for _ in range(n_e + 1)]
    step: list[list[tuple | None]] = [[None] * (n_g + 1) for _ in range(n_e + 1)]
    for j in range(n_g, -1, -1):
        best[n_e][j] = (n_g - j) * SKIP_COST
        step[n_e][j] = ("skip",) if j < n_g else None
    for i in range(n_e - 1, -1, -1):
        ex = exercises[i]
        one_sided = looks_unilateral(ex.title)
        for j in range(n_g, -1, -1):
            if j < n_g and SKIP_COST + best[i][j + 1] < best[i][j]:
                best[i][j], step[i][j] = SKIP_COST + best[i][j + 1], ("skip",)
            for sides in (1, 2):
                need = len(ex.sets) * sides
                if j + need > n_g:
                    continue
                cost = sum(
                    _rep_cost(h, active[j + k * sides + side])
                    for k, h in enumerate(ex.sets)
                    for side in range(sides)
                )
                if sides == 2 and not one_sided:
                    cost += PER_SIDE_COST * len(ex.sets)
                if sides == 1 and one_sided:
                    cost += PER_SIDE_COST * len(ex.sets)
                total = cost + best[i + 1][j + need]
                if total < best[i][j]:
                    best[i][j], step[i][j] = total, ("place", sides)

    pairs: list[tuple[dict[str, Any], HevySet]] = []
    per_side: list[str] = []
    skipped = 0
    i = j = 0
    while (i < n_e or j < n_g) and step[i][j]:
        move = step[i][j]
        if move[0] == "skip":
            skipped += 1
            j += 1
            continue
        sides = move[1]
        ex = exercises[i]
        for k, h in enumerate(ex.sets):
            for side in range(sides):
                pairs.append((active[j + k * sides + side], h))
        if sides == 2:
            per_side.append(ex.title)
        j += len(ex.sets) * sides
        i += 1

    if i < n_e or skipped > MAX_SKIPPED:
        return [], (
            f"left the sets alone: could not line up Garmin's {n_g} sets with Hevy's "
            f"{sum(len(e.sets) for e in exercises)}"
        )
    note = f"{len(pairs)} of {n_g} sets"
    if per_side:
        note += "; left + right for " + ", ".join(per_side)
    if skipped:
        note += f"; {skipped} extra Garmin set(s) left alone"
    return pairs, note


def apply_hevy_sets(pairs: list[tuple[dict[str, Any], HevySet]]) -> int:
    """Write Hevy weight (Garmin stores grams) and reps into the Garmin sets. Returns sets changed."""
    changed = 0
    for g, h in pairs:
        before = (g.get("weight"), g.get("repetitionCount"))
        if h.weight_kg is not None:
            g["weight"] = float(h.weight_kg) * 1000.0
        if h.reps is not None:
            g["repetitionCount"] = int(h.reps)
        after = (g.get("weight"), g.get("repetitionCount"))
        if after != before:
            changed += 1
            ex = (g.get("exercises") or [{}])[0]
            log.info("  %s/%s: weight %sg reps %s -> weight %sg reps %s",
                     ex.get("category"), ex.get("name"), *before, *after)
    return changed


def describe_token_file(path: Path) -> str:
    """Say what is wrong with a token file without printing any of it."""
    import json

    text = path.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(text)
    except ValueError:
        if text.count(".") == 2 and text.startswith("eyJ"):
            return (
                "the secret holds a single access token (a JWT), which expires within a day "
                "and cannot be refreshed. Run scripts/garmin_login.py and paste the JSON it prints."
            )
        return f"{path.name} is not JSON ({len(text)} characters, starts with {text[:1]!r})"
    if not isinstance(data, dict):
        return f"{path.name} is JSON but a {type(data).__name__}, not an object"
    missing = [k for k in ("di_token", "di_refresh_token", "di_client_id") if not data.get(k)]
    if missing:
        return f"{path.name} has keys {sorted(data)} but is missing {missing}"
    return f"{path.name} looks complete"


def normalise_token_file(path: Path) -> None:
    """Accept tokens pasted base64-encoded, as some Garmin login tools print them."""
    import base64
    import binascii
    import json

    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8").strip()
    try:
        json.loads(text)
        return
    except ValueError:
        pass
    try:
        decoded = base64.b64decode(text + "=" * (-len(text) % 4), validate=False).decode("utf-8")
        data = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return
    if isinstance(data, dict) and data.get("di_token"):
        path.write_text(json.dumps(data), encoding="utf-8")
        log.info("Garmin: decoded base64 tokens")
    elif isinstance(data, list):
        log.error(
            "Garmin: these are old garth tokens (OAuth1/OAuth2), which garminconnect "
            "0.3 no longer accepts. Run scripts/garmin_login.py and use its output."
        )


def garmin_login(tokenstore: str | None):
    from garminconnect import Garmin

    if tokenstore:
        normalise_token_file(Path(tokenstore) / "garmin_tokens.json")
    client = Garmin(os.getenv("GARMIN_EMAIL") or None, os.getenv("GARMIN_PASSWORD") or None)
    try:
        client.login(tokenstore)
    except Exception:
        token_file = Path(tokenstore or "") / "garmin_tokens.json"
        if tokenstore and token_file.is_file():
            log.error("Garmin login failed. %s", describe_token_file(token_file))
        raise
    if tokenstore:
        # Keep refreshed tokens so the next run does not need a password login.
        try:
            client.client.dump(tokenstore)
        except Exception as e:  # noqa: BLE001 - losing the cache is not fatal
            log.warning("Could not save Garmin tokens: %s", e)
    return client


def garmin_strength_activities(client, first: datetime, last: datetime) -> list[dict[str, Any]]:
    """Every Garmin strength activity from the day before `first` to the day after `last`."""
    activities = client.get_activities_by_date(
        (first.date() - timedelta(days=1)).isoformat(),
        (last.date() + timedelta(days=1)).isoformat(),
    )
    return [
        a for a in activities
        if (a.get("activityType") or {}).get("typeKey") in GARMIN_STRENGTH_TYPES
    ]


def sync_garmin(
    client, workout: HevyWorkout, candidates: list[dict[str, Any]], dry_run: bool
) -> dict[str, Any] | None:
    match = closest_by_start(candidates, garmin_start, workout.start)
    if not match:
        log.info("Garmin: no strength activity within %s of %s", MATCH_WINDOW, workout.start)
        return None

    aid = match["activityId"]
    if match.get("activityName") != workout.title:
        log.info("Garmin %s: rename %r -> %r", aid, match.get("activityName"), workout.title)
        if not dry_run:
            client.set_activity_name(aid, workout.title)
    else:
        log.info("Garmin %s: name already correct", aid)

    payload = client.get_activity_exercise_sets(aid) or {}
    sets = payload.get("exerciseSets") or []
    pairs, note = align_sets(sets, workout)
    changed = apply_hevy_sets(pairs)
    log.info("Garmin %s: matched sets %s; %d set(s) to update", aid, note, changed)
    if not pairs:
        for g in (s for s in sets if s.get("setType") == "ACTIVE"):
            ex = (g.get("exercises") or [{}])[0]
            log.info("  garmin set: %s/%s reps=%s duration=%s",
                     ex.get("category"), ex.get("name"), g.get("repetitionCount"), g.get("duration"))
    if changed and not dry_run:
        client.set_activity_exercise_sets(aid, {"activityId": aid, "exerciseSets": sets})
    return match


# --------------------------------------------------------------------------- #
# Intervals.icu
# --------------------------------------------------------------------------- #


class Intervals:
    def __init__(self, api_key: str, athlete_id: str = "0"):
        self.s = requests.Session()
        self.s.auth = ("API_KEY", api_key)
        self.athlete = athlete_id

    def activities(self, oldest: str, newest: str) -> list[dict[str, Any]]:
        """Activities between two dates (inclusive), Strava-sourced stubs left out."""
        r = self.s.get(
            f"{INTERVALS_API}/athlete/{self.athlete}/activities",
            params={"oldest": oldest, "newest": newest},
            timeout=30,
        )
        r.raise_for_status()
        return [a for a in r.json() if a.get("type")]  # Strava-sourced rows are empty stubs

    def update(self, activity_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        r = self.s.put(f"{INTERVALS_API}/activity/{activity_id}", json=fields, timeout=30)
        r.raise_for_status()
        return r.json()

    def original_file(self, activity_id: str) -> bytes:
        """The file the activity was imported from (Garmin's FIT), unzipped."""
        r = self.s.get(f"{INTERVALS_API}/activity/{activity_id}/file", timeout=60)
        r.raise_for_status()
        data = r.content
        return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data

    def time_stream(self, activity_id: str) -> list[float]:
        """Seconds from the activity's start for each sample, which interval indices point into."""
        r = self.s.get(f"{INTERVALS_API}/activity/{activity_id}/streams.json",
                       params={"types": "time"}, timeout=60)
        r.raise_for_status()
        return next((st.get("data") or [] for st in r.json() if st.get("type") == "time"), [])

    def intervals(self, activity_id: str) -> list[dict[str, Any]]:
        r = self.s.get(f"{INTERVALS_API}/activity/{activity_id}/intervals", timeout=30)
        r.raise_for_status()
        return r.json().get("icu_intervals") or []

    def set_intervals(self, activity_id: str, intervals: list[dict[str, Any]]) -> None:
        """Replace the activity's intervals."""
        r = self.s.put(f"{INTERVALS_API}/activity/{activity_id}/intervals",
                       params={"all": "true"}, json=intervals, timeout=60)
        r.raise_for_status()


def fit_sets(fit: bytes) -> list[dict[str, Any]]:
    """The `set` messages of a FIT file, oldest first, shaped like Garmin Connect's exerciseSets.

    Each has setType ("ACTIVE" or "REST"), repetitionCount, exercises[0].category,
    and the set's start (a UTC datetime) and duration in seconds.
    """
    from garmin_fit_sdk import Decoder, Stream

    decoder = Decoder(Stream.from_byte_array(bytearray(fit)))
    if not decoder.is_fit():
        return []  # a GPX or TCX upload: no sets to read
    messages, errors = decoder.read()
    if errors:
        raise ValueError(f"could not read the FIT file: {errors[0]}")
    sets = []
    for m in messages.get("set_mesgs") or []:
        duration = m.get("duration")
        start = m.get("start_time")
        if start is None and m.get("timestamp") and duration is not None:
            start = m["timestamp"] - timedelta(seconds=duration)
        if start is None or duration is None:
            continue
        category = m.get("category")
        if isinstance(category, list):
            category = category[0] if category else None
        sets.append({
            "setType": "ACTIVE" if m.get("set_type") == "active" else "REST",
            "repetitionCount": m.get("repetitions"),
            "exercises": [{"category": str(category).upper() if category else None, "name": None}],
            "start": start,
            "duration": float(duration),
        })
    sets.sort(key=lambda x: x["start"])
    return sets


def build_intervals(
    sets: list[dict[str, Any]],
    pairs: list[tuple[dict[str, Any], HevySet]],
    workout: HevyWorkout,
    activity_start: datetime,
    times: list[float],
) -> list[dict[str, Any]]:
    """One Intervals.icu interval per FIT set: WORK for an active set, labelled
    from the Hevy set it was paired with, and RECOVERY for a rest.

    Indices point into the activity's time stream; end_index is exclusive.
    The watch rounds set times, so a set can start a moment before the last one
    ended: each interval starts where the previous one ended at the earliest.
    Sets that fall outside the recording, or shorter than a sample, are left out.
    """
    exercise_of = {id(h): ex.title for ex in workout.exercises for h in ex.sets}
    hevy_for = {id(g): h for g, h in pairs}
    out: list[dict[str, Any]] = []
    for g in sets:
        offset = (g["start"] - activity_start).total_seconds()
        start_index = max(bisect.bisect_left(times, offset), out[-1]["end_index"] if out else 0)
        end_index = bisect.bisect_left(times, offset + g["duration"])
        if end_index <= start_index:
            continue
        if g["setType"] == "REST":
            kind, label = "RECOVERY", "Rest"
        else:
            kind = "WORK"
            h = hevy_for.get(id(g))
            if h is None:
                label = "Extra set (not in Hevy)"
            else:
                detail = fmt_set(h)
                label = exercise_of[id(h)] + (f": {detail}" if detail else "")
        out.append({"start_index": start_index, "end_index": end_index, "type": kind, "label": label})
    return out


def same_intervals(current: list[dict[str, Any]], wanted: list[dict[str, Any]]) -> bool:
    """Whether the activity already has the wanted intervals.

    Intervals.icu decides WORK or RECOVERY itself (it stores every strength set
    as WORK, whatever is sent), and fills any stretch of the recording that no
    interval covers with an unlabelled one. So only the labelled intervals'
    positions and labels are compared.
    """
    keys = ("start_index", "end_index", "label")
    return [tuple(i.get(k) for k in keys) for i in current if i.get("label")] == [
        tuple(i[k] for k in keys) for i in wanted]


def sync_intervals_laps(icu: Intervals, activity: dict[str, Any], workout: HevyWorkout,
                        dry_run: bool) -> None:
    """Turn the FIT file's sets into the activity's intervals, labelled from Hevy."""
    aid = activity["id"]
    sets = fit_sets(icu.original_file(aid))
    if not sets:
        log.info("Intervals %s: the activity's file has no FIT sets, intervals left alone", aid)
        return
    pairs, note = align_sets(sets, workout)
    if not pairs:
        log.info("Intervals %s: intervals left alone: %s", aid, note.removeprefix("left the sets alone: "))
        return
    wanted = build_intervals(sets, pairs, workout, parse_time(activity["start_date"]),
                             icu.time_stream(aid))
    current = icu.intervals(aid)
    if same_intervals(current, wanted):
        log.info("Intervals %s: intervals already match the sets", aid)
        return
    log.info("Intervals %s: replacing %d interval(s):", aid, len(current))
    for i in current:
        log.info("  %5s-%-5s %-8s %s", i.get("start_index"), i.get("end_index"), i.get("type"),
                 i.get("label") or "")
    log.info("Intervals %s: with %d interval(s) from the FIT sets (%s):", aid, len(wanted), note)
    for i in wanted:
        log.info("  %5d-%-5d %-8s %s", i["start_index"], i["end_index"], i["type"], i["label"])
    if not dry_run:
        icu.set_intervals(aid, wanted)


def intervals_changes(activity: dict[str, Any], workout: HevyWorkout) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if activity.get("name") != workout.title:
        changes["name"] = workout.title
    if workout.volume_kg and activity.get("kg_lifted") != workout.volume_kg:
        changes["kg_lifted"] = workout.volume_kg
    current = activity.get("description") or ""
    wanted = describe(workout)
    if current != wanted and (not current.strip() or current.startswith(DESCRIPTION_MARKER)):
        changes["description"] = wanted
    return changes


def sync_intervals(
    icu: Intervals,
    workout: HevyWorkout,
    garmin_activity: dict[str, Any] | None,
    acts: list[dict[str, Any]],
    dry_run: bool,
) -> bool:
    """Update the Intervals.icu activity matching `workout`. Returns whether one was found."""
    match = None
    if garmin_activity:
        gid = str(garmin_activity["activityId"])
        match = next((a for a in acts if str(a.get("external_id")) == gid), None)
    if not match:
        strength = [a for a in acts if a.get("type") in INTERVALS_STRENGTH_TYPES]
        match = closest_by_start(strength, lambda a: parse_time(a["start_date"]), workout.start)
    if not match:
        log.info("Intervals: no matching activity for %s", workout.start)
        return False

    changes = intervals_changes(match, workout)
    if not changes:
        log.info("Intervals %s: already up to date", match["id"])
    for k, v in sorted(changes.items()):
        if k == "description":
            log.info("Intervals %s: description ->\n%s", match["id"], v)
        else:
            log.info("Intervals %s: %s %r -> %r", match["id"], k, match.get(k), v)
    if changes and not dry_run:
        icu.update(match["id"], changes)
    sync_intervals_laps(icu, match, workout, dry_run)
    return True


# --------------------------------------------------------------------------- #


def load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines into the environment, leaving variables already set alone.

    Lets a local scheduler (systemd, launchd, Task Scheduler) run the sync with
    nothing more than a `.env` file beside it.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    load_env_file(Path(os.getenv("HEVY_SYNC_ENV_FILE") or Path(__file__).with_name(".env")))
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=int(os.getenv("LOOKBACK_DAYS", "2")),
                   help="sync Hevy workouts from the last N days (default 2)")
    p.add_argument("--all", action="store_true",
                   default=os.getenv("BACKFILL_ALL", "").lower() in {"1", "true", "yes"},
                   help="backfill: sync every Hevy workout ever recorded")
    p.add_argument("--since", type=date.fromisoformat, default=os.getenv("BACKFILL_SINCE") or None,
                   metavar="YYYY-MM-DD", help="backfill: sync every Hevy workout from this date on")
    p.add_argument("--dry-run", action="store_true",
                   default=os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"},
                   help="log what would change without changing anything")
    p.add_argument("--target", choices=TARGETS, default=os.getenv("SYNC_TARGET") or "both",
                   help="which service to update: garmin, intervals or both (default both)")
    p.add_argument("--skip-garmin", action="store_true", help="same as --target intervals")
    p.add_argument("--skip-intervals", action="store_true", help="same as --target garmin")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    want_garmin = args.target in {"both", "garmin"} and not args.skip_garmin
    want_intervals = args.target in {"both", "intervals"} and not args.skip_intervals
    if not (want_garmin or want_intervals):
        p.error("nothing to update: --target and --skip-* leave out both Garmin and Intervals.icu")
    # A service the user picked on its own must be configured; with "both", a
    # missing one is skipped so either half of the setup works by itself.
    required = args.target != "both"

    if args.all:
        since, label = None, "in your whole Hevy history"
    elif args.since:
        since = datetime.combine(args.since, datetime.min.time(), tzinfo=timezone.utc)
        label = f"since {args.since}"
    else:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        label = f"since {since.date()}"
    workouts = fetch_hevy_workouts(os.environ["HEVY_API_KEY"], since)
    log.info("Hevy: %d workout(s) %s%s", len(workouts), label, " (dry run)" if args.dry_run else "")
    if not workouts:
        return 0
    first, last = workouts[0].start, workouts[-1].start

    failures = 0
    garmin = None
    if want_garmin:
        tokenstore = os.getenv("GARMINTOKENS") or str(Path("~/.garminconnect").expanduser())
        has_tokens = (Path(tokenstore) / "garmin_tokens.json").is_file() or Path(tokenstore).is_file()
        if has_tokens or os.getenv("GARMIN_PASSWORD"):
            try:
                garmin = garmin_login(tokenstore)
            except Exception:  # noqa: BLE001 - still update Intervals
                failures += 1
                log.exception("Garmin login failed, skipping Garmin")
        elif required:
            failures += 1
            log.error("Garmin: target is garmin but no tokens or password are configured")
        else:
            log.warning("Garmin: no tokens or password configured, skipping Garmin")
    icu = None
    if want_intervals:
        if os.getenv("INTERVALS_API_KEY"):
            icu = Intervals(os.environ["INTERVALS_API_KEY"], os.getenv("INTERVALS_ATHLETE_ID", "0"))
        elif required:
            failures += 1
            log.error("Intervals.icu: target is intervals but no INTERVALS_API_KEY is configured")
        else:
            log.warning("Intervals.icu: no INTERVALS_API_KEY configured, skipping Intervals.icu")

    # Fetch each side's activities for the whole span once, rather than per workout:
    # a backfill over years would otherwise make thousands of list calls.
    garmin_acts: list[dict[str, Any]] = []
    if garmin:
        try:
            garmin_acts = garmin_strength_activities(garmin, first, last)
            log.info("Garmin: %d strength activities to match against", len(garmin_acts))
        except Exception:  # noqa: BLE001 - still update Intervals
            failures += 1
            garmin = None
            log.exception("Garmin: could not list activities, skipping Garmin")
    icu_acts: list[dict[str, Any]] = []
    if icu:
        try:
            icu_acts = icu.activities(
                (first.date() - timedelta(days=1)).isoformat(),
                (last.date() + timedelta(days=1)).isoformat(),
            )
            log.info("Intervals: %d activities to match against", len(icu_acts))
        except Exception:  # noqa: BLE001 - still update Garmin
            failures += 1
            icu = None
            log.exception("Intervals: could not list activities, skipping Intervals.icu")

    matched = {"garmin": 0, "intervals": 0}
    for n, w in enumerate(workouts):
        log.info("== %s  %s", w.start.isoformat(timespec="minutes"), w.title)
        if n and len(workouts) > BACKFILL_THRESHOLD:
            time.sleep(BACKFILL_PAUSE)  # be gentle with Garmin's unofficial API on a backfill
        g = None
        if garmin:
            try:
                g = sync_garmin(garmin, w, garmin_acts, args.dry_run)
                matched["garmin"] += g is not None
            except Exception:  # noqa: BLE001 - carry on to Intervals and other workouts
                failures += 1
                log.exception("Garmin update failed")
        if icu:
            try:
                matched["intervals"] += sync_intervals(icu, w, g, icu_acts, args.dry_run)
            except Exception:  # noqa: BLE001
                failures += 1
                log.exception("Intervals update failed")
    sides = [f"{matched['garmin']} on Garmin"] * want_garmin
    sides += [f"{matched['intervals']} on Intervals.icu"] * want_intervals
    log.info("Done: %d Hevy workout(s); matched %s; %d error(s)",
             len(workouts), ", ".join(sides), failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
