---
description: Read open issues for this checkout and check for an existing report before fixing a bug
---
Read open issues using the host's available issue interface: `read issue://?state=open&limit=100`.
For a known issue use `read issue://<N>`; for text search read `xd://github` and use
`search_issues` with the current checkout as the default repository. Do not hardcode
an owner/repository or infer issue state from an old task card.

If this host has no issue interface, the explicit CLI alternative is
`gh issue list --state open --limit 100`. Report lookup/authentication failures honestly;
an unavailable listing does not mean there are no open issues.

- Before fixing a bug, check for an existing report and cite its number in the handoff.
- There is no claim/reservation step. Do not assign issues, label them in-progress or
  post reservation comments. Board policy is `work/WORKFLOW.md`.
- Log out-of-scope bugs in the handoff; do not absorb them into the current change.
  Only file a report when asked: `/work-report` explains the user-invoked reporter,
  `gh issue create --title "<title>" --body-file <draft.md>` is the CLI alternative,
  and `work/backlog/` cards follow `work/WORKFLOW.md`.
- Never create issues, launch reporters or change board status as a listing side effect.

$ARGUMENTS
