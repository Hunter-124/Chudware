# work/questions/ — driver → advisor escalations

One file per task: `T-<id>.md`. Drivers append questions (with their recommended
answer + alternatives) and commit; the human relays to the Fable advisor session;
the advisor's `## ANSWER` section gets committed back. Decisions live in git, not
in anyone's context window.

Format per question:

```
## Q1 (open|answered) — short title

Context: 2-3 sentences.
Recommendation: what I'd do and why.
Alternatives: A / B.

## ANSWER (Q1)

Decision, rationale, what may now change.
```

## Archive

Resolved questions move to `archive/` once their task card reaches `status: done`
(one-line banner prepended noting the archive date and that the driver proceeded
on its recommended default). This directory (`work/questions/`, excluding
`archive/`) is the live register — only escalations for tasks that are still
open belong here. Files with no matching task card (e.g. standalone reports)
stay in place regardless of status.
