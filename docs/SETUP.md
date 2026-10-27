# Setup guide

This gets the daily sync running in your own GitHub account. It takes about
15 minutes. You will need:

- a GitHub account
- a **Hevy Pro** subscription (Hevy only offers its API to Pro members)
- an **Intervals.icu** account connected to Garmin Connect, so your activities
  are imported there automatically
- a **Garmin Connect** account
- Python 3.12 or newer on your own computer, used once for the Garmin login

Intervals.icu and Garmin are both optional: leave out one's secret and the job
skips it.

## 1. Get your own copy of the repository

Fork this repository (the **Fork** button at the top of its GitHub page), or
use **Use this template** if it is offered. Everything below happens in your
copy.

Forks start with GitHub Actions switched off. Open the **Actions** tab of your
copy and click **I understand my workflows, go ahead and enable them**.

## 2. Hevy API key

1. Sign in at <https://hevy.com> and open
   <https://hevy.com/settings?developer>.
2. Generate an API key and copy it.

## 3. Intervals.icu API key

1. In Intervals.icu open **Settings** and scroll down to **Developer Settings**.
2. Generate an API key if there isn't one yet, and copy it.

The job works on your own athlete, so you don't need your athlete id.

## 4. Garmin login token

Garmin has no API keys for personal use, so you log in once on your own
computer and give the job the login token that produces. Your password never
goes to GitHub.

```
pip install garminconnect==0.3.20
python scripts/garmin_login.py
```

(`scripts/garmin_login.py` is in your copy of the repository. Download it, or
clone the repository.)

It asks for your Garmin email and password, and for an MFA code if your account
uses one. Then it prints a single line like this:

```
{"di_token": "eyJ…", "di_refresh_token": "…", "di_client_id": "…"}
```

Copy **the whole line**, from the opening `{` to the closing `}`. The long
`eyJ…` value on its own is not enough. It expires within a day, and the job
needs the refresh token next to it to stay logged in.

Treat this line like a password: anyone who has it can read and edit your
Garmin account.

## 5. Add the secrets to GitHub

In your copy of the repository go to **Settings → Secrets and variables →
Actions → New repository secret**, and add:

| Name | Value |
|---|---|
| `HEVY_API_KEY` | the Hevy key from step 2 |
| `INTERVALS_API_KEY` | the Intervals.icu key from step 3 |
| `GARMIN_TOKENS` | the whole JSON line from step 4 |

The names must match exactly. (`HEVY_KEY` and `GARMIN_TOKEN` are accepted too.)

Optional: `GARMIN_EMAIL` and `GARMIN_PASSWORD` are tried only if the tokens stop
working. They do not help if your account uses MFA, and you can leave them out.

## 6. Try it without changing anything

1. Open **Actions → sync → Run workflow**.
2. Set **days** to `7`, tick **dry run**, and click **Run workflow**.
3. When it finishes, open the run, then the **sync** job, then the **Sync** step.

The log lists each Hevy workout and what would change on each side, for example:

```
INFO == 2026-10-25T07:22+00:00  Strength A — Lower + Plyo (Sunday)
INFO Garmin 1234567890: rename 'Strength A — Lower + Plyo (Sun' -> 'Strength A — Lower + Plyo (Sunday)'
INFO   SQUAT/None: weight 0.0g reps 6 -> weight 72000.0g reps 6
INFO Garmin 1234567890: matched sets 37 of 37 sets; left + right for Bulgarian Split Squat (Dumbbell); 25 set(s) to update
INFO Intervals i123456789: kg_lifted None -> 7353
INFO Intervals i123456789: name 'Strength A — Lower + Plyo (Sun)' -> 'Strength A — Lower + Plyo (Sunday)'
```

Garmin stores weights in grams, so `72000.0g` is 72 kg.

## 7. Turn it on

Run the workflow again with **dry run** unticked. That applies the changes for
the last 7 days.

After that it runs by itself every day at 22:00 UTC, and you never need to
touch it. To run it at another time, change the `cron` line in
`.github/workflows/sync.yml` (the time is in UTC).

## Troubleshooting

**"Garmin login failed. the secret holds a single access token (a JWT)…"**
Only the `eyJ…` value was pasted. Replace `GARMIN_TOKENS` with the whole JSON
line from step 4.

**"Garmin login failed" with anything else, or "Username and password are
required"**
The saved Garmin tokens no longer work. This can happen after a password change,
or if the job hasn't run for over a week and GitHub has cleared its cache. Run
`scripts/garmin_login.py` again and paste the new line into `GARMIN_TOKENS`. The
job notices when the secret changes and starts from the new tokens.

**"Garmin: no strength activity within 0:20:00 of …"**
The watch session started more than 20 minutes away from the Hevy workout, or
it isn't recorded as a strength activity (for example, it was recorded as
cardio). Start both at about the same time.

**"left the sets alone: could not line up Garmin's N sets with Hevy's M"**
The sets were too different to match safely, for example because you added
exercises in Hevy afterwards that the watch never recorded. The name is still
fixed. You can edit those sets in Garmin Connect by hand.

**Intervals.icu notes weren't updated**
That's deliberate if the activity already had notes you wrote. The job only
writes notes that are empty or that it wrote itself.

**The daily runs stopped**
GitHub pauses scheduled workflows in a public repository after 60 days without
a commit, and emails you when it does. Re-enable it from the Actions tab, or
make your copy private, which avoids the pause.

**Hevy returns 401 or 403**
The Hevy key is wrong, or your Hevy Pro subscription has lapsed.

## Privacy

- Each API key and the Garmin token are stored as GitHub secrets. They are
  masked in logs and visible to no one else.
- Refreshed Garmin tokens are kept in your repository's Actions cache. Only
  workflow runs in your repository can read them.
- The logs show workout names, exercise names, weights and reps. If your copy
  is public, anyone can read those logs. Make it private if that matters to you.
