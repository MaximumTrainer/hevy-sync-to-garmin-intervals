# garmin-hevy-intervals-sync

Log your lifting in [Hevy](https://www.hevyapp.com/), wear a Garmin watch while
you do it, and this job makes the Garmin Connect and
[Intervals.icu](https://intervals.icu/) copies of the session show what you
actually did. It runs once a day on GitHub Actions, costs nothing, and needs no
server.

## Why

A Garmin strength activity on its own often has:

- a **truncated name**: the watch cuts long workout names short, e.g.
  "Strength A — Lower + Plyo (Sun";
- **no weights**: every set is 0 kg unless you type each one in on the watch,
  so Garmin's total volume is meaningless;
- **guessed exercises and reps** from the watch's rep counter.

Intervals.icu imports that activity from Garmin, so it inherits the wrong name,
and its *kg lifted* field stays empty. Hevy has the right answer for all of
this. This job copies it across.

## What it changes

| | Garmin Connect | Intervals.icu |
|---|---|---|
| Name | Hevy's full workout title | Hevy's full workout title |
| Sets | the weight and reps Hevy recorded for each set | – |
| Weight | follows from the sets (Garmin's total volume) | **kg lifted** = Hevy's volume (weight × reps over every set, warm-ups included) |
| Notes | – | a set-by-set list, written only if the notes are empty or were written by this job |

Nothing else is touched: not the exercise names Garmin chose, not heart rate,
not training load, and never notes you wrote yourself.

Every run looks at the Hevy workouts from the last two days, so one missed run
catches up on the next. Running it again changes nothing that is already right,
and a dry-run mode shows every change it would make without making it.

## How it finds the matching activities

- **Garmin**: the strength activity that started within 20 minutes of the Hevy
  workout.
- **Intervals.icu**: the activity imported from that Garmin activity, or failing
  that, the strength activity that started within 20 minutes.

### How Hevy sets are lined up with Garmin sets

The two rarely have the same number of sets:

- Hevy logs a **one-sided exercise** (single-arm row, split squat, single-leg
  RDL…) as one set covering both sides. The watch records a set per side.
- The watch sometimes picks up a **stray set** that Hevy never saw.

So each Hevy exercise is placed on either one Garmin set per Hevy set or two
(left and right, each given the Hevy weight and reps), and stray Garmin sets are
skipped. Of all the ways to do that, the one whose rep counts agree best wins.
Exercises with words like "single", "split squat" or "lunge" in their name are
expected to be one-sided, but the rep counts can override that either way. If
the sets cannot be lined up the job leaves them alone, still fixes the name, and
says why in the log.

> **Dumbbells:** each side gets the full weight you typed into Hevy. If you log
> the *combined* weight of two dumbbells, the Garmin sets will show that total
> on each side.

## Get started

You need a GitHub account, a **Hevy Pro** subscription (Hevy's API is a Pro
feature), an **Intervals.icu** account, a **Garmin Connect** account, and
Python 3.12+ on your own computer for a one-off Garmin login.

Setup takes about 15 minutes. Follow **[docs/SETUP.md](docs/SETUP.md)**.

You don't have to use both: without Garmin credentials the job only updates
Intervals.icu, and `--skip-intervals` does the reverse.

## Running it on your own computer

```
pip install -r requirements.txt
export HEVY_API_KEY=… INTERVALS_API_KEY=…
python sync.py --dry-run --days 7      # show what would change
python sync.py --days 7                # change it
python -m pytest -q                    # tests
```

Options: `--days N` (how far back to look, default 2), `--dry-run`,
`--skip-garmin`, `--skip-intervals`. For Garmin, run `scripts/garmin_login.py`
once first; it saves its tokens to `~/.garminconnect`, where `sync.py` finds
them.

## Caveats

- Garmin Connect has no public API for editing activities. This uses
  [python-garminconnect](https://github.com/cyberjunky/python-garminconnect),
  which talks to the same endpoints as the Garmin Connect website. Garmin can
  change those at any time, so a run can start failing without warning.
- Your Garmin login tokens are kept in GitHub's Actions cache, which only your
  own workflow runs can read. Anyone with write access to your repository could
  change the workflow to read them, so don't give out write access lightly.
- This is a personal project, not affiliated with Hevy, Garmin or Intervals.icu.
