"""Copy Hevy workout details onto the matching Garmin Connect and Intervals.icu activities.

For each Hevy workout in the lookback window:

* Garmin Connect: the strength activity that started within MATCH_WINDOW of the
  Hevy workout gets Hevy's title (the watch truncates long workout names) and
  the weight and reps of every set Hevy recorded.
* Intervals.icu: the activity imported from that Garmin activity gets the same
  title, ``kg_lifted`` set to the Hevy volume, and a set-by-set breakdown in
  its description. A description someone wrote by hand is never overwritten.

Running it twice changes nothing the second time, so it is safe to re-run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("hevy-sync")

HEVY_API = "https://api.hevyapp.com/v1"
INTERVALS_API = "https://intervals.icu/api/v1"
MATCH_WINDOW = timedelta(minutes=20)
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


def fetch_hevy_workouts(api_key: str, since: datetime) -> list[HevyWorkout]:
    """Workouts that started at or after `since`, oldest first."""
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
        workouts.extend(w for w in batch if w.start >= since)
        # Hevy lists newest first, so once a page reaches past `since` we are done.
        if not batch or batch[-1].start < since or page >= body.get("page_count", page):
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


def describe(workout: HevyWorkout) -> str:
    lines = [DESCRIPTION_MARKER]
    for ex in workout.exercises:
        parts = []
        for s in ex.sets:
            if s.weight_kg and s.reps:
                p = f"{fmt_num(s.weight_kg)}kg×{s.reps}"
            elif s.reps:
                p = f"{s.reps} reps"
            elif s.duration_seconds:
                p = f"{fmt_num(s.duration_seconds)}s"
            else:
                continue
            parts.append(p + (" (w)" if s.set_type == "warmup" else ""))
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


def _blocks(items: list, key) -> list[list]:
    """Split a list into runs of consecutive items sharing the same key."""
    out: list[list] = []
    last = object()
    for item in items:
        k = key(item)
        if not out or k != last:
            out.append([])
            last = k
        out[-1].append(item)
    return out


def _garmin_exercise_key(s: dict[str, Any]) -> tuple:
    ex = (s.get("exercises") or [{}])[0]
    return (ex.get("category"), ex.get("name"))


def align_sets(
    garmin_sets: list[dict[str, Any]], workout: HevyWorkout
) -> tuple[list[tuple[dict[str, Any], HevySet]], str]:
    """Pair Garmin ACTIVE sets with Hevy sets.

    First by exercise (Garmin groups consecutive sets of one exercise), and if
    the exercise counts disagree, set by set when the totals agree. Returns the
    pairs and a note saying which strategy was used, or why nothing was paired.
    """
    active = [s for s in garmin_sets if s.get("setType") == "ACTIVE"]
    hevy_ex = [e for e in workout.exercises if e.sets]
    g_blocks = _blocks(active, _garmin_exercise_key)

    if len(g_blocks) == len(hevy_ex):
        pairs = []
        mismatched = []
        for gb, he in zip(g_blocks, hevy_ex):
            if len(gb) != len(he.sets):
                mismatched.append(f"{he.title} ({len(gb)} on Garmin, {len(he.sets)} in Hevy)")
            pairs.extend(zip(gb, he.sets))
        note = "by exercise"
        if mismatched:
            note += "; set counts differ for " + ", ".join(mismatched)
        return pairs, note

    hevy_flat = [s for e in hevy_ex for s in e.sets]
    if len(active) == len(hevy_flat):
        return list(zip(active, hevy_flat)), "set by set"

    return [], (
        f"left the sets alone: Garmin has {len(g_blocks)} exercises / {len(active)} sets, "
        f"Hevy has {len(hevy_ex)} exercises / {len(hevy_flat)} sets"
    )


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


def sync_garmin(client, workout: HevyWorkout, dry_run: bool) -> dict[str, Any] | None:
    day = workout.start.date()
    activities = client.get_activities_by_date(
        (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()
    )
    strength = [
        a for a in activities
        if (a.get("activityType") or {}).get("typeKey") in GARMIN_STRENGTH_TYPES
    ]
    match = closest_by_start(strength, garmin_start, workout.start)
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
        r = self.s.get(
            f"{INTERVALS_API}/athlete/{self.athlete}/activities",
            params={"oldest": oldest, "newest": newest},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def update(self, activity_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        r = self.s.put(f"{INTERVALS_API}/activity/{activity_id}", json=fields, timeout=30)
        r.raise_for_status()
        return r.json()


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
    icu: Intervals, workout: HevyWorkout, garmin_activity: dict[str, Any] | None, dry_run: bool
) -> None:
    day = workout.start.date()
    acts = [
        a for a in icu.activities(
            (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()
        )
        if a.get("type")  # Strava-sourced rows come back as empty stubs
    ]
    match = None
    if garmin_activity:
        gid = str(garmin_activity["activityId"])
        match = next((a for a in acts if str(a.get("external_id")) == gid), None)
    if not match:
        strength = [a for a in acts if a.get("type") in INTERVALS_STRENGTH_TYPES]
        match = closest_by_start(strength, lambda a: parse_time(a["start_date"]), workout.start)
    if not match:
        log.info("Intervals: no matching activity for %s", workout.start)
        return

    changes = intervals_changes(match, workout)
    if not changes:
        log.info("Intervals %s: already up to date", match["id"])
        return
    for k, v in sorted(changes.items()):
        if k == "description":
            log.info("Intervals %s: description ->\n%s", match["id"], v)
        else:
            log.info("Intervals %s: %s %r -> %r", match["id"], k, match.get(k), v)
    if not dry_run:
        icu.update(match["id"], changes)


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=int(os.getenv("LOOKBACK_DAYS", "2")),
                   help="sync Hevy workouts from the last N days (default 2)")
    p.add_argument("--dry-run", action="store_true",
                   default=os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"},
                   help="log what would change without changing anything")
    p.add_argument("--skip-garmin", action="store_true")
    p.add_argument("--skip-intervals", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    workouts = fetch_hevy_workouts(os.environ["HEVY_API_KEY"], since)
    log.info("Hevy: %d workout(s) since %s%s", len(workouts), since.date(),
             " (dry run)" if args.dry_run else "")
    if not workouts:
        return 0

    failures = 0
    garmin = None
    if not args.skip_garmin:
        tokenstore = os.getenv("GARMINTOKENS") or str(Path("~/.garminconnect").expanduser())
        has_tokens = (Path(tokenstore) / "garmin_tokens.json").is_file() or Path(tokenstore).is_file()
        if has_tokens or os.getenv("GARMIN_PASSWORD"):
            try:
                garmin = garmin_login(tokenstore)
            except Exception:  # noqa: BLE001 - still update Intervals
                failures += 1
                log.exception("Garmin login failed, skipping Garmin")
        else:
            log.warning("Garmin: no tokens or password configured, skipping Garmin")
    icu = None
    if not args.skip_intervals:
        icu = Intervals(os.environ["INTERVALS_API_KEY"], os.getenv("INTERVALS_ATHLETE_ID", "0"))

    for w in workouts:
        log.info("== %s  %s", w.start.isoformat(timespec="minutes"), w.title)
        g = None
        if garmin:
            try:
                g = sync_garmin(garmin, w, args.dry_run)
            except Exception:  # noqa: BLE001 - carry on to Intervals and other workouts
                failures += 1
                log.exception("Garmin update failed")
        if icu:
            try:
                sync_intervals(icu, w, g, args.dry_run)
            except Exception:  # noqa: BLE001
                failures += 1
                log.exception("Intervals update failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
