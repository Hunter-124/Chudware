---
id: T-BL53
title: Discord bot /stats + /leaderboard — implement the git-derived stats/embed helpers in notify_discord.py
workstream: BL
owner: gissa
status: dropped
branch: task/T-BL53-discord-stats
depends_on: []
lane: platform-infra
kind: new
priority: 3
deferred: no
---

## Context

The Gateway Discord bot (`work/tools/discord_bot.py`, currently untracked dev/ops WIP)
registers slash commands `/stats`, `/update`, `/screenshot`, `/leaderboard` and imports
five helpers from `notify_discord.py` that DO NOT yet exist in the committed module:
`_compute_stats`, `_stats_embed`, `_leaderboard_embed`, `_digest_embed`, `_recent_screenshots`.
So the stats/leaderboard commands cannot run. The leaderboard is **git-author-derived**
(no stored scoreboard); `identity_aliases` in `work/sync/notify_config.json` folds a
contributor's commit identities to one canonical name.

This task implements those helpers, additively, in `notify_discord.py` — no change to the
existing notify/poll/hook logic. It does NOT build the bot bootstrap (`discord_bot.py` also
needs `nd._get`, a REST-GET helper for guild resolution — that's separate Gateway-bot work,
out of scope here) and does NOT commit the untracked `discord_bot.py` (leave that to its owner).

## Steps / stages

1. `_compute_stats(main_worktree, days, identity_aliases)` -> `(commits, owners)`:
   per-author commit counts over N days (git-author-derived, aliases folded) + active
   task-ownership counts from `work.py board --json` (status in doing/review).
2. `_stats_embed(commits, owners, days)` -> Discord embed dict (top committers + owners).
3. `_leaderboard_embed(main_worktree, cfg)` -> Discord embed dict (all-time commit leaderboard,
   aliases folded, ranked with medals).
4. `_digest_embed(main_worktree, cfg, period_days, label)` -> `(embed_dict, shots)`:
   commits + task activity + board/Master-Plan progress over the period, plus recent shots.
5. `_recent_screenshots(main_worktree, n)` -> list[Path] of the most recent git-tracked
   images under `work/questions/` (shared helper needed by `_digest_embed` and `/screenshot`).

## Definition of done

- [ ] The five helpers exist in `notify_discord.py`; existing logic untouched.
- [ ] Embeds validate via `discord.Embed.from_dict` (rendered locally against real git data).
- [ ] `discord_bot.py`'s stats/leaderboard/digest/screenshot imports all resolve.
- [ ] No edits to other lanes; `discord_bot.py` left untracked for its owner.

## AI hints

- Stats/leaderboard use `git log --all` so unmerged-branch commits count immediately.
- Reuse `nd._git`, `nd._main_worktree`, `nd.COLORS`, `nd._roadblocks`; match the `_embed` dict shape.

## Progress log
- 2026-07-06 21:38 UTC [gissa] [gissa/opus-sweep] 2026-07-06 â€” DONE, in review. Branch task/T-BL53-discord-stats pushed (commit 3a777d2f, +161 lines, purely additive to notify_discord.py â€” zero edits to existing notify/poll/hook logic). Implemented the 5 helpers discord_bot.py imports: _compute_stats, _stats_embed, _leaderboard_embed, _digest_embed, _recent_screenshots (+ small private helpers). All git-author-derived (git log --all so unmerged-branch commits count); owners from work.py board --json (doing/review); leaderboard folds identity_aliases.

TESTED locally against real git data with discord.py 2.7.1: discord.Embed.from_dict validated all 3 embeds (stats/leaderboard/digest), no empty field values. Sample: all-time leaderboard = Hunter-124 2506, gissa 105, opus-wynn 38; 7-day stats total 2155 commits/5 contributors; daily digest 186 commits, board 200/241 done, +3 screenshots. All bot stats/leaderboard/digest/screenshot imports now resolve.

OUT OF SCOPE (flagged, not done): discord_bot.py's bootstrap still needs nd._get (REST-GET guild resolution) to actually launch the Gateway process â€” that's separate bot-startup work, not part of the stats feature. discord_bot.py + requirements-discord-bot.txt remain UNTRACKED (left for their owner; not committed here). NOT merged, NOT done.
- 2026-08-04 [audit] Dropped at Hunter's direction 2026-08-04; the Discord bot is
  cancelled. (Note: `status: dropped` is not a value `work/tools/work.py` accepts —
  STATUSES = {todo, doing, review, done, blocked, backlog} — so this card is filed
  as `status: backlog` to keep it inert/out-of-rotation while recording the
  cancellation here. Source files are NOT deleted; the tooling-deletion decision is
  separate and still open.)
