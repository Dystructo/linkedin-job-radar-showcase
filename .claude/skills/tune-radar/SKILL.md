---
name: tune-radar
description: Weekly tuning for Job Radar. Reads the tracker rows the user marked (not_relevant, skip, notes) plus run_log and per-query stats, then proposes specific edits to config.yaml queries and term lists or to jobradar/rubric.md. Shows a diff and applies nothing until the user approves.
disable-model-invocation: true
argument-hint: "[days, default 14]"
---

# tune-radar: propose tuning from the user's marks

Interactive. You propose; the user decides. Apply nothing until they say yes to a specific diff.
No em dashes in anything you write.

## Python command

`PY` is `.venv\Scripts\python.exe` on the user's Windows PC if `.venv` exists, else `python3`
(or `python`).

## 1. Gather evidence

Use a window of $ARGUMENTS days if a number was given, else 14.

- `PY -m jobradar tracker --marked`: rows where the user set `status` (applied, dm_sent, skip,
  not_relevant) or wrote `notes`. If the Sheet is unreachable, say so and continue with stats.
- `PY -m jobradar stats --days <N>`: Apify spend, per-day counts, prefilter drop reasons,
  kept-by-location, per-query judged / top picks / average score, role categories, caps applied.
- Read `data/judged/<date>.jsonl` for the window when you need the full judgment behind a row
  (match on `post_url`), and recent `prefilter` entries in `data/run_log.jsonl` (`drop_details`)
  to spot good posts that were dropped.
- Read `config.yaml` and `jobradar/rubric.md` so proposals edit what is really there.

## 2. Diagnose

Look for patterns, not single cases. Useful questions:

- **False positives**: what do `not_relevant` and `skip` rows share (role category, poster type,
  location, a phrase)? Was the score too high because of a rubric gap, or did a query pull in
  the wrong kind of post?
- **Notes**: what did the user say they want more or less of?
- **Wins**: what do `applied` and `dm_sent` rows share? Which queries found them?
- **Queries**: which queries produce zero top picks over the window (candidates to rewrite or
  drop) and which produce most of them (candidates for a variant)? Apify credit is limited: the
  budget is `apify.monthly_post_budget`, so a new query should usually replace a weak one.
- **Prefilter**: is a drop reason suspiciously large (for example `location_other` or
  `no_hiring_term`)? Check a few `drop_details` for good posts dropped by mistake. Is a term
  missing from a list?

## 3. Propose

Present at most 5 proposals, most valuable first. For each:

- What changes (file and exact lines) and why, citing the evidence (row counts, example post
  URLs, query stats).
- The expected effect and any risk (for example "may drop some Gurugram posts that only say
  Haryana").
- A unified diff of the change.

Rules for proposals:

- Queries: at most 500 characters and at most 5 AND/OR/NOT operators each (config load checks
  this). Keep the total query count times `max_posts_per_query` times 30 under the monthly
  budget.
- Term lists match whole words, case-insensitive; add plurals separately.
- Rubric changes must keep the output fields and enum values unchanged, because code validates
  them. Change guidance and scoring examples, not the schema.

## 4. Apply only after approval

Ask which proposals to apply. For each approved one, make the edit, then run
`PY -m unittest discover -s tests -t .`. If tests fail, fix the edit or revert it and tell the
user. Then summarise what changed. Do not commit or push unless the user asks.
