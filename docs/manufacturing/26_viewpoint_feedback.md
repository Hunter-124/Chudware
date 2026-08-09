# Viewpoint Stage-2 Feedback Protocol

## Purpose and boundary

This protocol is the scarce, alignment-bearing half of the learned-viewpoint curriculum. Stage 1 asks the CAD kernel and renderer questions with exact physical answers: whether the target feature is in frame, occluded, foreshortened, large enough, or silhouette-informative. Stage 2 asks the consumer of the rendered image the question physics cannot answer: **did this view let the consumer perform the task?** Stage-1 oracle values may generate candidates and pretrain a policy, but they are not usefulness labels. Likewise, Stage-2 judgments do not replace geometric oracle labels.

The harness renders multiple poses, randomizes their presentation, and calls a scorer adapter. The scorer is the same kind of model that will consume the selected view. No scorer implementation or network dependency lives in this package: `Scorer.score(CandidateSetResponse) -> ScoringResponse` is the only call seam, and the offline tests use scripted scorers.

The implementation is dependency-free under `services/viewpoint/feedback/`.

## Wire schemas

Every object has `schema_version: "1.0"`. Deserializers reject missing and unknown fields rather than silently supplying provenance defaults.

### Candidate-set request

`CandidateSetRequest` is the renderer-facing request:

| Field | Meaning |
|---|---|
| `request_id` | Unique scoring-request identity |
| `geometry_hash` | Lowercase SHA-256 of the geometry snapshot |
| `feature` | Stable target feature reference |
| `task_text` | Exact task the image consumer must perform |
| `candidate_poses[]` | At least two unique `{candidate_id, pose}` entries |
| `randomization_seed` | Integer in `[0, 2^63)` used only by a local PRNG |

A `CameraPose` contains finite three-vectors `eye`, `target`, and nonzero `up`, plus `fov_y_degrees` in `[1, 180)`. Eye and target must differ.

### Candidate-set response / offer

After rendering, `build_candidate_set()` validates that there is exactly one render for every requested candidate and that its pose is unchanged. `CandidateSetResponse` records:

- the request ID, geometry hash, feature, and exact task text;
- `canonical_candidates[]`, each carrying candidate ID, full pose, render URI, and render SHA-256;
- `randomization_seed`;
- `permutation[]`, a bijection over canonical candidate indices.

The scorer sees `ordered_candidates`, which is the canonical list indexed by the recorded permutation. Replaying the request with the same seed reproduces the order. A private `random.Random(seed)` instance is used, so global process RNG state cannot perturb it.

### Scoring response

The scorer returns both signal families in one `ScoringResponse`:

1. **Pairwise preference:** every unordered candidate pair appears exactly once and in presentation order as `{candidate_a_id, candidate_b_id, preferred_id, confidence, abstain_reason}`. `preferred_id` must name A or B and confidence is 1–4. If the pair cannot be distinguished, `preferred_id` is null, confidence is zero, and a nonempty abstention reason is mandatory.
2. **Per-candidate rubric:** one 0–4 integer for each of `feature_visible`, `unoccluded`, `unforeshortened`, `adequate_scale`, and `task_usefulness`.

A model unable to evaluate the set may use `overall_abstain_reason`; that response has no pairwise scores and therefore can never create a preference label. Rubric scores are optional on an overall abstention because forcing a rubric would merely relocate the false judgment.

A non-abstained response must cover every candidate once in the rubric and every pair once in the pairwise list. The pair orientation must match the randomized display order; this makes positional behavior measurable rather than reconstructing it after the fact.

### Which signal wins

**Pairwise preference is authoritative for winner/loser labels.** It won because a within-task A/B decision demands less absolute calibration, exposes ties through abstention, and aggregates across raters without pretending that one model's “3” is another model's “3.” The rubric remains required because it diagnoses why a view was useful, supplies a bounded training weight (`rubric_margin`), and reveals contradictions or curriculum gaps. It never overrides the pairwise winner. Rubric-only labels would be more vulnerable to scale-use drift; pairwise-only records would provide too little diagnosis when policy training fails.

The intended scorer instruction is: inspect the target feature only for the exact task; score every rubric independently; compare every pair; prefer a candidate only when it materially improves the ability to answer; abstain rather than infer hidden geometry; do not reward image aesthetics unrelated to the task.

## Durable preference record

`PreferenceRecord` seals the offer and score with:

- record and request IDs;
- geometry hash, feature, exact task text;
- canonical candidate IDs, poses, render URIs, render hashes, display permutation, and seed;
- rubric and pairwise scores, confidence, and abstention reasons;
- scorer `model_id` and `harness_version`;
- timezone-aware ISO-8601 timestamp;
- `answer_correct: true | false | null`, where null means correctness was not checkable;
- publication consent/licensing provenance or explicit null.

`JsonlPreferenceStore.append()` writes canonical one-record-per-line JSON using append mode, flushes it with `fsync`, and strict-loads every line. A malformed or provenance-incomplete line fails loading with its line number; it is not partially accepted.

When downstream answer correctness is checkable, the harness must set it after verification. Default aggregation rejects an explicitly wrong answer. Null is retained because null means “not checkable,” not “correct.”

## Bias and agreement controls

### Position bias

Candidate identity is randomized for every request and the permutation is stored. Pair A is always the earlier displayed member. `detect_position_bias()` counts decisive A wins and computes the exact two-sided binomial p-value under $p(A)=0.5$. A scorer is flagged when it has at least eight decisive comparisons and $p < 0.05$ by default. Aggregation removes every record from a flagged scorer before computing labels. The adversarial test scorer always chooses A; across eight seeded requests both candidate identities occupy the first slot, yet all eight A wins produce $p=0.0078125$ and are rejected.

This control detects a consistent first-slot heuristic; it does not prove that an unflagged scorer is unbiased. Production collection should use enough randomized repeats per scorer/task family to reach the configured minimum.

### Inter-rater agreement

For each `(geometry_hash, feature, task_text, unordered candidate pair)`, abstentions are treated as missing judgments. `inter_rater_agreement()` reports

$$A = \frac{\text{unordered rater pairs choosing the same winner}}{\text{unordered rater pairs with decisive judgments}}.$$

It returns null when fewer than two decisive judgments are comparable, 1 for unanimous decisions, and 0 for a two-rater disagreement. This observed agreement is intentionally transparent at small sample sizes; the result also carries the underlying agreeing/compared counts rather than presenting a chance-corrected statistic as stable with two raters.

## Aggregation into training data

`aggregate_preferences()` performs the following clean cut in order:

1. Strict records are deduplicated by record ID, then by `(request_id, model_id, canonical scoring response)`. Reusing one record ID with conflicting content is an error.
2. Scorers with statistically detected first-position bias are excluded.
3. Overall/full-pair abstentions are excluded without creating labels.
4. Records with a checkable incorrect downstream answer are excluded by default.
5. Publication exports additionally apply the release predicate below.
6. Decisive votes are grouped by exact task and unordered candidate pair. A group needs at least two raters and observed agreement at or above 0.67 by default. Ties and low-agreement groups are excluded and counted.
7. The majority pairwise choice becomes `winner`; the other render becomes `loser`. The example carries geometry/feature/task provenance, both full render candidates and poses, average rubric usefulness margin, mean pairwise confidence, agreement, and all source record IDs.

`AggregationResult` includes examples and counts for duplicates, abstentions, known-wrong answers, position-biased records, insufficient-rater groups, low-agreement groups, and publication-ineligible records. Exclusion is therefore auditable rather than silent.

## Publication predicate

Private training retention and public redistribution are separate decisions. `publication_eligible()` returns true only when:

- a `PublicationConsent` object exists;
- `export_restricted` is false and `redaction_applied` is true;
- the license kind is `owned`, `licensed`, or `synthetic` (third-party is denied by default);
- `model_training` and `public_redistribution` are both true;
- `license_ref` occurs in the release review's explicit approved-license set;
- `consent_ref` occurs in the release review's explicit approved-consent set.

A nonempty reference is never self-approval. Missing consent, an empty approval set, or a redistribution-restricted record is refused. `aggregate_preferences(..., for_publication=True, ...)` applies this predicate before producing a release dataset.

## Verification

The hardware-, GPU-, network-, and model-free protocol suite is:

```bash
python3 -X utf8 -m pytest services/viewpoint/feedback -q
```

It round-trips strict schemas, rejects missing provenance, proves seeded order replay, catches an always-first adversarial scorer, proves abstention cannot emit a label, checks agreeing and disagreeing inter-rater cases, filters low agreement, refuses publication without consent, and round-trips the fsynced JSONL store.
