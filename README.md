# LinkedIn Job Radar

Every morning this finds new LinkedIn hiring posts for Founder's Office, CEO's Office, Chief of
Staff, EIR, strategy and VC investment roles in Delhi NCR (or remote India), throws out junk and
scams, has Claude score what is left, and leaves you a short digest plus a tracker sheet.

It never logs into LinkedIn and never applies, messages or comments for you. It only finds,
scores and reports.

```
7:37 AM IST  Claude routine (cloud)
             fetch posts (Apify, no login) -> filter in code -> Claude scores -> digest + tracker
             -> commits to your private GitHub repo
9:07 AM IST  Optional backup (GitHub Actions + Gemini), runs only if the routine did not
```

What you get each day:
- **Digest**: `data/digests/<date>.md` in your GitHub repo. Top picks (score 7+), worth a look
  (5 to 6, plus unknown-location posts), and counts of what was filtered.
- **Tracker**: a Google Sheet with every post scoring 5+. You fill in `status` and `notes`.

See [docs/sample-digest.md](docs/sample-digest.md) for an example digest, generated from the
made-up test posts in `tests/fixtures/`.

## About this repository

This is the public showcase copy of a pipeline I run every day. The live instance runs in a
private repository, because its `data/` folder fills up with other people's LinkedIn posts. This
copy contains only code, docs and fictional test data.

Design choices worth a look:

- **No-login scraping only.** Posts come from an Apify actor that needs no LinkedIn account,
  cookies or session, so nothing can get an account restricted.
- **Cheap code before expensive AI.** `jobradar/prefilter.py` dedupes, drops job-seeker posts,
  hard scams (fees, deposits, Telegram links, including links hidden behind post text) and
  other-city posts before anything reaches the judge. Every drop is counted in a run log.
- **One rubric, two judges.** Claude (inside a scheduled Claude Code routine) and a Gemini
  fallback both score against `jobradar/rubric.md`. Their output is validated against a strict
  schema, re-judged once if invalid, and the rubric's hard caps are re-applied in code, so a
  judge mistake can never push a scam or an off-target post to the top.
- **Safe secrets.** In the cloud routine the Apify token is a network secret that a proxy adds to
  outgoing requests, so the model never sees it. Locally, secrets live only in `.env`.
- **Budget guard.** A monthly post budget and a per-run cost cap keep the scraper inside the
  free tier.
- **Idempotent daily runs.** Re-running on the same day merges into that day's digest and never
  duplicates tracker rows or re-judges posts it has already seen.
- **Tested offline.** `python -m unittest discover -s tests -t .` runs 93 tests with zero network
  calls, including an end-to-end fixture run and a same-day re-run.

To run your own copy, follow the setup below and keep your repository **private**.

---

## Setup overview

| Step | What | Time |
|---|---|---|
| 1 | Apify account and token | 5 min |
| 2 | Install Git, create a private GitHub repo, push the project | 15 min |
| 3 | Connect Claude Code to GitHub | 5 min |
| 4 | Local test runs, then the Google Sheet tracker | 15 min |
| 5 | Create the daily routine | 15 min |
| 6 | Optional: GitHub Actions backup with Gemini | 10 min |
| 7 | Daily use | |
| 8 | Costs, limits and troubleshooting | |

Commands below are for Windows PowerShell, run inside the project folder. Set up the Python
environment once with `python -m venv .venv` and
`.venv\Scripts\python.exe -m pip install -r requirements.txt`. In the Claude desktop app you can
also just ask Claude to run them for you.

---

## 1. Apify account and token

1. Sign up at https://console.apify.com/sign-up (free plan, no card needed).
2. Copy your **Personal API token** from https://console.apify.com/settings/integrations.
3. Put it in the `.env` file in the project folder: `APIFY_TOKEN=apify_api_...`
   (`.env` is never uploaded to GitHub.)

The free plan gives $5 of credit each month. This project is set to stay under about $4.40.

## 2. Private GitHub repo

**2a. Install Git.** In PowerShell:

```bash
winget install --id Git.Git -e --source winget
```

Or download it from https://git-scm.com/download/win and click through the installer with the
default options. Close and reopen PowerShell (or the Claude app) afterwards.

**2b. Create the repo.**
1. Sign in (or sign up) at https://github.com.
2. Open https://github.com/new.
3. Repository name: `linkedin-job-radar`. Choose **Private**. Leave "Add a README" and the other
   options unticked.
4. Click **Create repository** and keep the page open; you need its URL
   (`https://github.com/<your-username>/linkedin-job-radar.git`).

**2c. Push the project.** Replace `<your-username>`:

```bash
git init -b main
```

```bash
git add .
```

```bash
git commit -m "Initial commit: LinkedIn Job Radar"
```

```bash
git remote add origin https://github.com/<your-username>/linkedin-job-radar.git
```

```bash
git push -u origin main
```

The first push opens a browser window to sign in to GitHub. Afterwards, refresh the GitHub page:
you should see the files, and `.env` must NOT be among them.

## 3. Connect Claude Code to GitHub

The routine runs in Anthropic's cloud and needs access to your private repo.

1. Open https://claude.ai/code and sign in with your Claude Pro account.
2. If asked to connect GitHub, do it, and when GitHub asks which repositories the **Claude** app
   may access, choose **Only select repositories** and pick `linkedin-job-radar`.
3. If you were not asked, install the app directly: https://github.com/apps/claude, click
   **Configure** (or **Install**), and add `linkedin-job-radar`.

## 4. Test runs and the Google Sheet

**4a. Fixture test (no internet, no cost).**

```bash
.venv\Scripts\python.exe -m jobradar run --fixture --fresh
```

It should end with a digest of 6 top picks in `.fixture_data/digests/`. That folder is only for
testing.

**4b. Run the tests (optional).**

```bash
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

**4c. Google Sheet tracker.**
1. Create a new sheet at https://sheets.new and name it `Job Radar Tracker`.
2. **Extensions > Apps Script**. Delete the sample code, paste the whole of
   `jobradar/apps_script.gs`, and click **Save**.
3. Left sidebar: **Project Settings** (gear) > **Script properties** > **Add script property**.
   Property `SHARED_SECRET`, value: the `SHEET_SHARED_SECRET` value from your `.env` (Claude
   generates one for you; otherwise make up a long random string). **Save**.
4. Back in the **Editor**, choose the `setup` function in the toolbar dropdown and click **Run**.
   Approve the permission prompt (Google warns that the app is unverified because you wrote it:
   **Advanced > Go to project**). A `Tracker` tab with headers appears in the sheet.
5. **Deploy > New deployment**. Gear icon > **Web app**. Execute as: **Me**. Who has access:
   **Anyone**. Click **Deploy** and copy the **Web app URL** (ends in `/exec`).
6. Put it in `.env`: `SHEET_WEBAPP_URL=https://script.google.com/macros/s/.../exec`
7. Check the connection:

```bash
.venv\Scripts\python.exe -m jobradar tracker
```

It should print `Google Sheet: 0 rows`.

"Anyone" can reach the URL, but every request must carry your secret, and the script can only
add rows or read them. It never deletes or edits your rows.

**4d. One real run on your PC (optional).** In the Claude desktop app, open this folder and type
`/scan-posts`. Claude fetches, filters, judges, writes the digest and pushes to GitHub, exactly
like the routine will.

## 5. Create the daily routine

Everything here is also listed in `docs/routine-prompt.md`.

1. Open https://claude.ai/code/routines and click **New routine** (or, in the Claude desktop app's
   Code tab: **Routines** > **New routine** > **Cloud**).
2. **Name**: `Job Radar daily`.
3. **Instructions**: paste the prompt from `docs/routine-prompt.md` (the text inside the box).
   In the model selector next to it, pick **Sonnet**.
4. **Repositories**: add `linkedin-job-radar`.
5. **Environment**: click the cloud icon below the instructions (it shows **Default**), then
   create a new environment named `job-radar` and open its settings (hover, gear icon):
   - **Network access**: **Custom**. Allowed domains, one per line:
     `api.apify.com`, `script.google.com`, `script.googleusercontent.com`.
     Tick **Also include default list of common package managers**.
   - **Network secrets** > **Add secret**: Name `Apify`, Secret type **Bearer**, Allowed websites
     `api.apify.com`, header `Authorization` with prefix `Bearer`, value: your Apify token.
     Click **Add**.
   - **Environment variables**:
     ```
     SHEET_WEBAPP_URL=<your web app URL>
     SHEET_SHARED_SECRET=<your secret>
     ```
   - **Setup script**: `python3 -m pip install --quiet requests pyyaml`
   - **Save changes**, and make sure the routine uses `job-radar`.
6. **Select a trigger**: **Schedule**, **Daily**, time **7:37 AM** (your local time, IST).
7. **Connectors** (bottom of the form): remove all of them. This routine needs none.
8. Click **Create**, then **Run now** on the routine's page.
9. Open the run and read the end of the transcript. A green status only means the session ran;
   the report at the end tells you whether the job worked. Then check GitHub for a new commit
   "Job Radar <date>: N top picks" and the digest in `data/digests/`.

Why it is set up this way:
- The Apify token is a **network secret**: the cloud proxy adds it to calls to `api.apify.com`
  only, and Claude never sees it.
- The Sheet secret must be a plain environment variable (Apps Script cannot read request
  headers). It can only add rows to your tracker, and only you use this environment.
- 7:37 rather than 7:00, because runs scheduled exactly on the hour can start several minutes late.

## 6. Optional: GitHub Actions backup (Gemini)

If the routine fails on a given day, this runs at 9:07 AM IST with Gemini as the judge. It skips
itself whenever today's digest already exists, so it never spends Apify credit twice.

1. Get a free Gemini API key: https://aistudio.google.com/apikey. (On the free tier Google may use
   what you send to improve its products; here that is only public LinkedIn post text.)
2. In your repo on GitHub: **Settings > Secrets and variables > Actions**.
   - **Secrets** tab > **New repository secret**, once for each: `APIFY_TOKEN`, `GEMINI_API_KEY`,
     and if you use the Sheet, `SHEET_WEBAPP_URL` and `SHEET_SHARED_SECRET`.
   - **Variables** tab > **New repository variable**: `MODE_B_ENABLED` = `true`.
3. Test it: **Actions** tab > **Job Radar (Mode B fallback)** > **Run workflow** (tick **force**
   if today's digest already exists).

To pause the backup, set `MODE_B_ENABLED` to `false`.

## 7. Daily use

**Read the digest.** On your phone: GitHub app > your repo > `data` > `digests` > today's file.
On a computer: `https://github.com/<your-username>/linkedin-job-radar/tree/main/data/digests`.

**Mark the tracker.** In the Sheet, set `status` for each row you act on:

| status | meaning |
|---|---|
| `applied` | you applied |
| `dm_sent` | you messaged the poster |
| `skip` | real and relevant, but you are passing |
| `not_relevant` | the radar got it wrong (this is what tuning learns from) |

Add a few words in `notes` when something is off ("needs 3 years", "actually Bangalore").

**Weekly: `/tune-radar`.** In the Claude desktop app, open this folder and type `/tune-radar`.
Claude reads your marks and the stats, proposes up to 5 changes to queries, filter terms or the
rubric, shows the exact diff, and changes nothing until you approve. Afterwards, commit and push
(ask Claude to do it).

**Outreach: `/draft-outreach <post link>`.** Claude researches the company and drafts a LinkedIn
connection note, a DM and a cold email in your voice, saved in `outreach/`. Nothing is sent. Fill
in `profile.md` once so the drafts have your real details.

**Useful commands**

| Command | Use |
|---|---|
| `.venv\Scripts\python.exe -m jobradar stats --days 14` | What each query and filter did |
| `.venv\Scripts\python.exe -m jobradar tracker --marked` | Rows you have marked |
| `.venv\Scripts\python.exe -m jobradar fetch --dry-run` | Show exactly what would be sent to Apify |

## 8. Costs, limits and troubleshooting

**Costs**

| Item | Cost |
|---|---|
| Apify | About $0.002 per post. 12 queries x up to 6 posts x 30 days = at most 2,160 posts, about $4.30, inside the $5 monthly free credit. A guard in `config.yaml` (`monthly_post_budget: 2200`) shrinks or skips fetches near the limit, and each run is capped at $0.25. |
| Claude | Uses your Pro usage once a day (judging up to 60 posts). No extra charge. |
| Gemini (optional) | Free tier. |
| GitHub, Google Sheets | Free. |

**Limits to know**
- LinkedIn search has no location filter; NCR posts whose location is only in an image may show
  as "Location unknown". They are kept, not dropped.
- At most 60 posts are judged per day (NCR first). Extras are counted as "over the daily cap" and
  can come back the next day.
- Routines are a research preview, so screens may look slightly different from these steps.

**Troubleshooting**

| Symptom | Fix |
|---|---|
| Digest note "Fetch failed: Apify rejected the token (401)" | Routine: re-add the network secret for `api.apify.com`. Locally: check `APIFY_TOKEN` in `.env`. |
| "The cloud environment blocked api.apify.com" | Add `api.apify.com` to the environment's allowed domains (step 5). |
| Digest note "Monthly Apify budget reached" | Expected near month end. It resets on the 1st (IST). |
| No new commit on GitHub after a run | Open the run transcript. Check the Claude GitHub App has access to the repo (step 3) and that `main` has no branch protection blocking pushes. |
| Tracker rows missing, log says "saved ... for the next run" | The Sheet was unreachable. Rows are kept in `data/tracker_unsent.csv` and sent on the next successful run. Check `SHEET_WEBAPP_URL`, the secret, and that the web app is deployed with access "Anyone". |
| "Sheet rejected the request: unauthorized" | `SHARED_SECRET` in Script properties and `SHEET_SHARED_SECRET` differ. |
| You edited `apps_script.gs` | Deploy > Manage deployments > edit > Version: **New version** > Deploy. The URL stays the same. |
| Too many irrelevant posts, or good ones missing | Mark rows in the Sheet for a week, then run `/tune-radar`. |
| Want to run it right now | Routine page > **Run now**, or `/scan-posts` in the desktop app. |

**Where things live**: `config.yaml` (every tunable value), `jobradar/rubric.md` (how posts are
scored), `CLAUDE.md` (project brief for Claude), `docs/routine-prompt.md` (routine prompt),
`.github/workflows/daily.yml` (backup), `data/` (digests, judged posts, run log, seen list).
