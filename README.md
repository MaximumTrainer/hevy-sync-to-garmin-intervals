# hevy-sync-to-garmin-intervals

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
| Intervals | – | one interval per set on the timeline, labelled with the Hevy exercise, weight and reps, with a recovery interval for each rest, so you see heart rate per exercise ([how](#sets-as-intervalsicu-intervals)) |

Nothing else is touched: not the exercise names Garmin chose, not heart rate,
not training load, and never notes you wrote yourself.

You choose what it updates: Garmin and Intervals.icu (the default), or only
one of them. See [Choosing what to update](#choosing-what-to-update).

Every run looks at the Hevy workouts from the last two days, so one missed run
catches up on the next. To bring older sessions into line as well, run a
[backfill](#backfilling-your-history) once. Running it again changes nothing that is already right,
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

### Sets as Intervals.icu intervals

Intervals.icu doesn't show a strength session's sets on its timeline, but the
FIT file your watch recorded has them. Garmin writes a `set` message for every
set, active or rest, with its start time, duration and rep count (see
[this Intervals.icu forum thread](https://forum.intervals.icu/t/strength-training-exercises-as-intervals-laps-on-the-timeline-hr-per-exercise-possible/128015/4)).
The job:

1. downloads the activity's original FIT file from Intervals.icu and reads its
   `set` messages;
2. lines up the active sets with the Hevy sets, the same way as for Garmin
   (above);
3. turns each set into an interval at the same point in the recording,
   labelled e.g. `Bench Press (Barbell): 42kg×10` (or `Extra set (not in
   Hevy)` for a set Hevy doesn't have), and each rest into one labelled `Rest`.
   They are sent as WORK and RECOVERY, but Intervals.icu classifies intervals
   itself and shows strength sets and rests alike as WORK, so go by the label;
4. replaces the activity's intervals with these, so Intervals.icu works out the
   heart rate, time and load of each one.

This reads the FIT file Intervals.icu already has, so it works without Garmin
(`target: intervals`) too. If the sets cannot be lined up, or the activity
wasn't recorded as a FIT file, the intervals are left as they are. Intervals you
drew by hand on a strength activity are replaced once its sets line up. Running
it again leaves intervals that already match alone.

## Choosing what to update

| Target | Updates | Needs |
|---|---|---|
| `both` (default) | Garmin Connect and Intervals.icu | the Garmin tokens and `INTERVALS_API_KEY` |
| `garmin` | Garmin Connect only | the Garmin tokens |
| `intervals` | Intervals.icu only | `INTERVALS_API_KEY` |

- **One run from GitHub Actions:** pick **target** under **Actions → sync → Run
  workflow**.
- **The daily run:** in your repository go to **Settings → Secrets and
  variables → Actions → Variables** and add a variable `SYNC_TARGET` set to
  `garmin` or `intervals`. Without it the daily run updates both.
- **On your own computer:** pass `--target garmin` or `--target intervals`, or
  put `SYNC_TARGET=garmin` in `.env`.

With `intervals`, the job doesn't log in to Garmin at all and finds each
Intervals.icu activity by its start time alone, so you don't need to set up
Garmin. With `garmin` or `intervals`, the run fails if that service's
credentials are missing, so a broken setup doesn't go unnoticed. With `both`,
a service with no credentials is skipped with a warning.

## Backfilling your history

The daily run only looks back two days. A **backfill** applies the same
changes to every Hevy workout you have ever logged, or to every workout from a
date you choose. Each workout is matched to its Garmin and Intervals.icu
activity exactly as in the daily run, so the names, set weights, kg lifted and
notes come out the same as if the job had been running all along.

What to expect:

- **Workouts with nothing to match are skipped.** If there was no Garmin
  strength activity within 20 minutes of a Hevy workout (you didn't wear the
  watch, or recorded it as another activity type), the log says so and moves
  on. Intervals.icu is still updated if it has its own strength activity at
  that time.
- **It is safe to repeat.** Anything already right is left alone, so you can
  run a backfill again, or stop one halfway and start it again.
- **It is slower.** Every workout is a few calls to Garmin, and the job pauses
  a second between workouts to stay gentle on Garmin's unofficial API. Expect
  roughly a minute per 30 to 40 workouts. Garmin and Intervals.icu activities
  are each listed once for the whole period, not once per workout.
- **Do a dry run first.** On a large history, read the log before letting it
  write. The last line sums the run up: how many Hevy workouts it found, how
  many it matched on each side, and how many errors it hit.

### From GitHub Actions

**Actions → sync → Run workflow**, then either:

- tick **all_history** to process every Hevy workout ever recorded, or
- type a date such as `2025-01-01` into **since** to process every workout
  from that date on.

Leave **days** as it is; a backfill ignores it. Tick **dry run** the first time,
check the log, then run it again with **dry run** unticked. The job is allowed
up to two hours, enough for several years of training. For longer histories,
backfill a year at a time with **since**, or run it from your own computer.

### From your own computer

```
.venv/bin/python sync.py --all --dry-run          # every workout: show what would change
.venv/bin/python sync.py --all                    # every workout: change it
.venv/bin/python sync.py --since 2025-01-01       # every workout from 1 Jan 2025
```

On a local run there is no time limit. `--target garmin` or
`--target intervals` limits a backfill to one side, on GitHub Actions as well
as locally.

## Get started

You need a GitHub account, a **Hevy Pro** subscription (Hevy's API is a Pro
feature), an **Intervals.icu** account, a **Garmin Connect** account, and
Python 3.12+ on your own computer for a one-off Garmin login.

Setup takes about 15 minutes. Follow **[docs/SETUP.md](docs/SETUP.md)**.

You don't have to use both: see
[Choosing what to update](#choosing-what-to-update) to update only Garmin or
only Intervals.icu.

## Running it on your own computer

You don't need GitHub Actions. The job runs fine from your own computer, and you
can run it by hand or have the computer run it every day.

### One-off setup (all platforms)

1. Install Python 3.12 or newer and git, then get the code and a virtual
   environment:

   ```
   git clone https://github.com/<you>/hevy-sync-to-garmin-intervals.git
   cd hevy-sync-to-garmin-intervals
   python3 -m venv .venv                     # Windows: py -3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt # Windows: .venv\Scripts\pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` in the same folder and fill in your
   `HEVY_API_KEY` and `INTERVALS_API_KEY` (see [docs/SETUP.md](docs/SETUP.md),
   steps 2 and 3, for where to get them). `sync.py` reads `.env` by itself, so
   no scheduler needs to know your keys. `.env` is git-ignored. Keep it to your
   own user account (`chmod 600 .env` on Linux and macOS).

3. Log in to Garmin once:

   ```
   .venv/bin/python scripts/garmin_login.py        # Windows: .venv\Scripts\python scripts\garmin_login.py
   ```

   This saves the tokens to `~/.garminconnect` (`%USERPROFILE%\.garminconnect`
   on Windows). `sync.py` uses them from there and keeps them refreshed. The
   line the script prints is only needed for GitHub Actions.

4. Try it:

   ```
   .venv/bin/python sync.py --dry-run --days 7      # show what would change
   .venv/bin/python sync.py --days 7                # change it
   ```

Options: `--days N` (how far back to look, default 2), `--all` or
`--since YYYY-MM-DD` (a [backfill](#backfilling-your-history)), `--dry-run`,
`--target both|garmin|intervals` (see
[Choosing what to update](#choosing-what-to-update)). With the default
`both`, any service left out of `.env` is skipped.
To keep `.env` somewhere else, point `HEVY_SYNC_ENV_FILE` at it.

### Run it every day as a service

The sync does its work in a few seconds and exits, so the right shape is a
**daily scheduled job**, not a program that stays running. Each platform's own
scheduler does this, and each can catch up on a run missed while the computer
was off or asleep.

Below, replace `/path/to/hevy-sync-to-garmin-intervals` with where you cloned the
repository. Every example runs at 22:00 local time.

#### Linux (systemd user timer)

Create `~/.config/systemd/user/hevy-sync.service`:

```ini
[Unit]
Description=Sync Hevy workouts to Garmin Connect and Intervals.icu
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/hevy-sync-to-garmin-intervals
ExecStart=/path/to/hevy-sync-to-garmin-intervals/.venv/bin/python sync.py
```

and `~/.config/systemd/user/hevy-sync.timer`:

```ini
[Unit]
Description=Run the Hevy sync every day

[Timer]
OnCalendar=*-*-* 22:00:00
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
```

Then:

```
systemctl --user daemon-reload
systemctl --user enable --now hevy-sync.timer
loginctl enable-linger "$USER"          # keep running when you're logged out
systemctl --user start hevy-sync        # run it once now
journalctl --user -u hevy-sync          # read the log
systemctl --user list-timers hevy-sync  # see when it runs next
```

`Persistent=true` runs a missed job as soon as the machine is back on. No
systemd? Add a cron line instead (`crontab -e`):
`0 22 * * * cd /path/to/hevy-sync-to-garmin-intervals && .venv/bin/python sync.py >> sync.log 2>&1`

#### macOS (launchd agent)

Create `~/Library/LaunchAgents/com.github.hevy-sync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.github.hevy-sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/hevy-sync-to-garmin-intervals/.venv/bin/python</string>
    <string>sync.py</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/hevy-sync-to-garmin-intervals</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>22</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/path/to/hevy-sync-to-garmin-intervals/sync.log</string>
  <key>StandardErrorPath</key><string>/path/to/hevy-sync-to-garmin-intervals/sync.log</string>
</dict>
</plist>
```

Then:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.github.hevy-sync.plist
launchctl kickstart gui/$(id -u)/com.github.hevy-sync   # run it once now
tail sync.log                                           # read the log
```

If the Mac is asleep at 22:00, launchd runs the job when it wakes. To stop it:
`launchctl bootout gui/$(id -u)/com.github.hevy-sync`.

Don't keep the clone in Desktop, Documents or Downloads. macOS stops background
jobs reading those folders unless you grant them Full Disk Access.

#### Windows (Task Scheduler)

In PowerShell, from the repository folder:

```powershell
$repo   = (Get-Location).Path
$action = New-ScheduledTaskAction -Execute "$repo\.venv\Scripts\python.exe" `
            -Argument "sync.py" -WorkingDirectory $repo
$daily  = New-ScheduledTaskTrigger -Daily -At 22:00
$opts   = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "Hevy sync" -Action $action -Trigger $daily -Settings $opts `
  -Description "Sync Hevy workouts to Garmin Connect and Intervals.icu"

Start-ScheduledTask -TaskName "Hevy sync"     # run it once now
Get-ScheduledTaskInfo -TaskName "Hevy sync"   # LastTaskResult 0 means it worked
```

`-StartWhenAvailable` runs a missed job as soon as the PC is back on. The task
runs as you, while you're signed in. To run it when you're signed out too, open
Task Scheduler, find **Hevy sync**, open Properties, and choose **Run whether
user is logged on or not**. To keep a log, use
`-Execute "cmd.exe" -Argument "/c .venv\Scripts\python.exe sync.py >> sync.log 2>&1"`
instead. To remove the task: `Unregister-ScheduledTask -TaskName "Hevy sync"`.

### Running the tests

```
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The tests decode `tests/fixtures/strength.fit`, a small Garmin-style strength
recording. `tests/fixtures/make_strength_fit.py` rebuilds it (it needs
`pip install fit-tool`, which the job itself doesn't).

## Caveats

- Garmin Connect has no public API for editing activities. This uses
  [python-garminconnect](https://github.com/cyberjunky/python-garminconnect),
  which talks to the same endpoints as the Garmin Connect website. Garmin can
  change those at any time, so a run can start failing without warning.
- Your Garmin login tokens are kept in GitHub's Actions cache, which only your
  own workflow runs can read. Anyone with write access to your repository could
  change the workflow to read them, so don't give out write access lightly.
- This is a personal project, not affiliated with Hevy, Garmin or Intervals.icu.

## License

[MIT](LICENSE)
