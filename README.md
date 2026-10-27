# garmin-hevy-intervals-sync

A daily GitHub Actions job that copies each Hevy workout onto the matching
Garmin Connect and Intervals.icu activities.

| | Garmin Connect | Intervals.icu |
|---|---|---|
| Name | Hevy's full title (the watch truncates long workout names) | Hevy's full title |
| Weight | the weight and reps of every set, so Garmin's total volume is right | `kg_lifted` = Hevy volume (weight × reps over all sets) |
| Notes | – | set-by-set breakdown in the description, only if the description is empty or was written by this job |

It looks at Hevy workouts from the last two days, so a missed run catches up.
Activities are matched by start time (within 20 minutes); the Intervals
activity is found through the Garmin activity it was imported from. Re-running
changes nothing that is already right.

## How the sets are matched on Garmin

Garmin groups the watch's sets by exercise. When Garmin and Hevy have the same
number of exercises, sets are paired exercise by exercise; otherwise, when the
total number of sets agrees, set by set. If neither lines up the Garmin sets are
left alone (the name is still fixed) and the log says why. Exercise names on
Garmin are never changed.

## Setup

1. **Hevy**: an API key from <https://hevy.com/settings?developer> (needs Hevy Pro).
2. **Intervals.icu**: an API key from Settings → Developer Settings.
3. **Garmin**: on your own computer run
   `pip install garminconnect==0.3.20 && python scripts/garmin_login.py`
   and copy the JSON it prints.
4. Add the repository secrets (Settings → Secrets and variables → Actions):
   `HEVY_API_KEY`, `INTERVALS_API_KEY`, `GARMIN_TOKENS`.
   `GARMIN_EMAIL` and `GARMIN_PASSWORD` are optional: they are used only if the
   tokens stop working, and will not help on an account with MFA.
5. Actions → sync → Run workflow, with **dry run** ticked, and read the log.
   Untick it once the planned changes look right.

The job runs at 22:00 UTC. GitHub pauses scheduled workflows in a public
repository after 60 days without a commit; a private repository avoids that.

## Running locally

```
pip install -r requirements.txt
HEVY_API_KEY=… INTERVALS_API_KEY=… python sync.py --dry-run --days 7
python -m pytest -q
```
