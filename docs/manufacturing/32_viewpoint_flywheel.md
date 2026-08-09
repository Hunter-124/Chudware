# Viewpoint judgement flywheel

## What this closes

`services.viewpoint.flywheel` is an explicit, opt-in loop from a part question to durable preference
and answer-outcome data:

1. A `FlywheelJob` identifies the geometry hash, feature, question, exact camera poses, seeded
   presentation order, document, optional usefulness answer key, and frozen-oracle diagnostics.
2. `render_candidate_sheet` in `services.viewpoint.render` renders every canonical pose through the
   supplied `RenderBackend`. The resulting `CandidateSetResponse` is the feedback package's exact
   schema; the flywheel does not add model hints to it or alter its recorded permutation.
3. One or more `ScorerBackend` implementations receive that candidate set and its images. They
   return all rubric rows, every pairwise comparison in presentation order, the candidate actually
   used to answer, and the answer text.
4. The loop calls feedback's `score_candidate_set` for schema validation and sealing, then calls
   `aggregate_preferences` over the durable records. Pairwise votes remain authoritative; rubric
   values are diagnostics only. Abstentions, exact-binomial position bias, low agreement,
   duplicates, and known-wrong answers retain feedback's existing behavior.
5. A usefulness-backed job checks the answer against `QuestionSpec.correct_answer` after scoring.
   The answer key and derivation are never included in the scorer prompt. The durable record keeps
   both the preference winner and `answer_correct`, because a view a model says it wants can still
   produce a wrong answer.

Nothing runs on import, at application startup, or from the test suite. The CLI must be invoked
explicitly.

## Durable outputs

`JsonlFlywheelStore(<root>)` writes:

- `records.jsonl`: append-only, flushed and `fsync`ed `chudware.viewpoint.flywheel/1` records;
- `dry_runs.jsonl`: append-only rendered-sheet manifests and per-scorer cost estimates; and
- `aggregated.json`: atomically replaced `AggregationResult.examples`, counts, and the current
  preference-versus-correctness comparison.

A full record includes the untouched feedback `PreferenceRecord`; canonical candidates, PNG hashes,
seed and permutation; render request digests and engine revision/capability/backend provenance;
question/source identifiers; per-candidate frozen-oracle aggregate and seven components when the
question came from usefulness; chosen candidate, answer and correctness; provider and exact model
id; flywheel harness version; per-call prompt/completion/total tokens; pricing assumptions; USD
cost; and whether accounting was provider-reported or estimated. Publication eligibility remains
gated by feedback's `PublicationConsent` and approved references rather than by the presence of a
record.

Repeated identical answers from one model are intentionally de-duplicated by feedback. Supply at
least two independent scorer/model ids to obtain the default two-rater agreement needed for a
training example. Repetition still has value for position-bias measurement, but it is not presented
as independent inter-rater evidence.

## Hardware-free scripted run

This exercises the real sheet assembler, exact feedback schema, durable store, answer check, and
aggregator with deterministic PNGs and no provider:

```bash
python3 -X utf8 -m services.viewpoint.flywheel \
  --output scratch/viewpoint-flywheel-demo --raters 2
```

The two scripted rater ids agree, producing three pairwise preference examples for the three-view
sheet. The adversarial control runs one slot-A scorer over eight independently seeded permutations:

```bash
python3 -X utf8 -m services.viewpoint.flywheel \
  --output scratch/viewpoint-flywheel-bias --first-position-biased
```

Expected counts are `input_records=8`, `position_biased_records=8`, and zero examples. Candidate
identity does change in the first presented position; rejection is based on the exact binomial slot
control, not a fixed candidate name.

To make preference/correctness disagreement visible on offline data, append one correct and one
incorrect scripted sheet to the same store:

```bash
python3 -X utf8 -m services.viewpoint.flywheel \
  --output scratch/viewpoint-flywheel-outcome --seed 10 --raters 2
python3 -X utf8 -m services.viewpoint.flywheel \
  --output scratch/viewpoint-flywheel-outcome --seed 11 --raters 2 --scripted-answer no
```

Both scorers still prefer and select `view-a`; the combined comparison reports two
preferred-and-correct and two preferred-but-incorrect records, with preferred-view accuracy 0.5.
Feedback's training aggregation excludes the known-wrong records by default, but the durable raw
records retain them for the hypothesis test.

The CLI's `DemoRenderBackend` exists only for a hardware-free plumbing smoke. A meaningful model run
must pass a real `WorkerRpcRenderBackend` advertising `doc_render_view.camera_pose.v1` to
`run_flywheel`; do not interpret the demo's 1x1 PNG as viewpoint evidence.

## Binding the usefulness benchmark

`FlywheelJob.from_usefulness(item, candidate_poses=..., geometry_hash=..., doc_id=...,
randomization_seed=...)` consumes the sibling `BenchmarkItem` API. It copies the public prompt and
choices into `CandidateSetRequest.task_text`, binds `CandidateView.observation.candidate_id` to the
supplied exact render poses, and records `CandidateView.aggregate` plus all frozen-oracle
components. Evaluation-only `correct_answer`, derivation, measurements, and feature-marker ids are
stored as provenance but are not sent to the scorer. Thus the same record supports both questions:

- which view did the model prefer; and
- did answering from the selected view work?

`compare_preference_correctness` reports checkable/correct/incorrect counts, accuracy among chosen
views, accuracy among selections agreeing with the pairwise winner, preferred-but-incorrect count,
and Pearson correlation between chosen-view oracle aggregate and correctness when the sample has
variation. The larger usefulness benchmark remains the authoritative experiment for the population
relationship; the flywheel comparison is a provenance-preserving operational view.

## Exact live-model configuration

The live adapter uses `services.agent.config.get_provider`; it does not implement an HTTP client and
does not own credentials. First configure/sign in to the desired provider through the existing
Chudware AI Settings/provider plumbing. Keep API keys and OAuth tokens in that existing credential
store. Do **not** put a secret in the flywheel JSON.

Create a JSON file with exactly these eight keys (prices and image-token accounting must match the
provider/model terms you intend to use):

```json
{
  "enabled": true,
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "max_output_tokens": 1800,
  "input_usd_per_million": 3.0,
  "output_usd_per_million": 15.0,
  "image_tokens_per_candidate": 1200,
  "chars_per_token": 4.0
}
```

Then there are two independent spending opt-ins:

1. the file must contain `"enabled": true`; and
2. live execution must construct `RealModelScorer(config, allow_live=True)`, or the CLI must receive
   `--allow-live-model`.

For the CLI plumbing smoke (which still uses demo pixels):

```bash
python3 -X utf8 -m services.viewpoint.flywheel \
  --live-model-config scratch/viewpoint-live-model.json \
  --allow-live-model --output scratch/viewpoint-live-smoke --raters 1
```

`--live-model-config` is repeatable for independent scorer/model configurations. Without
`--allow-live-model`, a non-dry live command refuses before provider resolution. During pytest,
`RealModelScorer.judge` refuses unconditionally even if both opt-ins are present. Provider creation
is lazy, so dry-run estimation cannot start the OMP sidecar or make a model request.

For a meaningful real run, construct jobs from usefulness items, construct the repository's exact
worker render backend, and call:

```python
result = run_flywheel(
    jobs,
    scorer=(real_scorer_a, real_scorer_b),
    render_backend=exact_camera_worker_backend,
    output_dir="scratch/viewpoint-flywheel-live",
)
```

The caller owns document creation/selection and worker connection. The flywheel neither starts an
engine nor initializes CUDA, avoiding the OCCT/TBB-in-a-CUDA-process tessellation landmine.

## Dry-run cost accounting

Dry-run renders and persists every sheet but never calls `judge`:

```bash
python3 -X utf8 -m services.viewpoint.flywheel \
  --live-model-config scratch/viewpoint-live-model.json \
  --sheets 50 --raters 2 --dry-run \
  --output scratch/viewpoint-flywheel-cost
```

Requests are exact: sheets times configured scorers times raters. Estimated input tokens are
`ceil(model-facing text characters / chars_per_token) + candidates *
image_tokens_per_candidate` per request. Estimated completion tokens reserve
`max_output_tokens` per request. USD cost applies the configured per-million prices. The manifest
marks all of these values `estimated=true`; it does not mislabel them as provider-reported usage.
A live record instead uses the provider plumbing's reported prompt/completion totals when available
and configured prices, falling back to the same explicitly marked estimate only when the provider
does not expose usage.

With the example configuration above, 50 three-view sheets and two ratings per sheet report 100
requests, 441,600 estimated prompt tokens, 180,000 reserved completion tokens, 621,600 total tokens,
and $4.0248. That is an estimate under the supplied token/pricing assumptions, not a vendor quote.

## What remains unproven

The scripted loop proves wiring, provenance, controls, and separation of preference from outcome; it
does not prove that any live model's preferences predict correctness. Only an explicitly funded run
on real rendered benchmark parts, with independent raters and the usefulness analysis, can measure
that relationship. A weak, zero, or negative relationship is a valid result and must not be tuned
away.
