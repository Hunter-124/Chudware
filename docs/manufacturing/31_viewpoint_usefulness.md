# Viewpoint usefulness benchmark

## Question being tested

The frozen geometric oracle measures whether a named feature is inspectable; it does not measure whether that view lets a vision model answer a part question. This benchmark makes the second event observable. Every trial is a multiple-choice visual question with a single answer recomputed from the seeded procedural geometry. The model-facing boundary contains only the question, choices, marker legend, candidate ID, and annotated RGB image. The correct answer, derivation measurements, frozen-oracle components, and aggregate stay on the evaluation side.

The five catalogue families are:

| Question family | Full 522-part catalogue | Ground-truth derivation |
|---|---:|---|
| visible hole count | 522 | Project mesh vertices into the marked feature plane, retain the local face neighbourhood, cluster distinct radii at a fixed 14% of the feature radius, and classify one versus two-or-more circular boundaries. |
| pocket/bore throughness | 522 | A marked recess is through exactly when another hole opening has a parallel axis, transverse centre offset no greater than 20% of the marked radius, and nonzero axial separation. Otherwise it is blind. |
| boss height comparison | 397 | For both marked cylindrical regions, retain mesh vertices within 3.2 local radii and eight axial radii, measure axial max-minus-min extent, and compare A with B. A stable part-ID permutation prevents marker order from leaking the answer. |
| counterbore concentricity | 466 | Require parallel axes and measure the transverse centre offset. The pair is concentric when offset is at most 5% of the smaller geometry-fitted radius. The corpus includes both same-bore end pairs and deliberately non-coaxial pairs. |
| diameter ratio | 522 | Fit each marked circular feature radius from its geometry landmarks, compute A/B (diameter ratio equals radius ratio), and assign it to predeclared bins `<0.75`, `0.75 to 1.33`, or `>1.33`. Marker order alternates by part index. |
| **Total** | **2,429** | Each stored `QuestionSpec.measurements` records the numeric inputs used for its answer. |

Counts above are from:

```text
nice -n 15 env OMP_NUM_THREADS=6 MKL_NUM_THREADS=6 \
  python3 -X utf8 -m services.viewpoint.usefulness --parts 522 --candidates 16
```

A question is emitted only when one of that part's six existing pixel-corpus rows targets the question's primary feature. Consequently all 2,429 questions inherit an actual candidate pose set and the frozen component vector for that same feature; the two comparison marker may name a second feature, but the row and oracle label always key the primary `QuestionSpec.feature_id`. The 522-part count and existing 6-by-16 corpus therefore yield 38,864 candidate-level usefulness trials without generating a pose or changing frozen geometry/oracle numerics.

## Image-only contract

`PublicQuestion` has no answer, derivation, geometry, measurements, feature ID, oracle component, or aggregate. `CandidateObservation` has only `candidate_id` and `uint8 HxWx3` pixels. `bind_pixel_corpus` copies the existing render and draws constant-size cyan/magenta identity rings by projecting the named feature centres with the stored exact-perspective camera. Ring size, colour, and shape never depend on the answer. A real model adapter implements:

```python
answer(question: PublicQuestion, observation: CandidateObservation) -> str
```

and must return one declared choice. Evaluation-only `QuestionSpec` and `CandidateView` retain the answer and oracle labels outside that call.

## Existing-corpus binding

`build_question_catalog(part_indices, examples_per_part=6, seed=1729)` reconstructs the same deterministic `generate_part_examples` ordering used by `services.viewpoint.pixels.corpus`. Every question records `part_id`, `part_index`, `example_index`, and primary `feature_id`. `bind_pixel_corpus(catalog, corpus)` resolves `(part_id, example_index)` to the rendered row, annotates each RGB candidate, and attaches the canonical components in order:

`coverage, visibility, foreshortening, scale, silhouette, depth, uniqueness`.

The aggregate is recomputed with the frozen oracle's `aggregate_quality`, not a new usefulness weighting. The binder is hardware-free when handed a materialized/cache-loaded `PixelCorpus`; it does not call the engine, tessellate OCCT geometry, initialize CUDA, or import Torch.

## Scripted measuring-instrument controls

Three deterministic answerers exercise the exact public answer method without a network, model, API key, engine, GPU, or Torch:

- `OracleAnswerer`: hidden answer lookup; must be exactly 100% correct.
- `BlindAnswerer`: stable samples from declared per-question-kind priors and never reads the image; with uniform priors it must land at chance and show no component dependence.
- `ComponentDegradedAnswerer(component=name)`: hidden test control. For a question with $K$ choices and named component quality $q$, it answers correctly with probability
  $$P(\mathrm{correct}\mid q)=\frac{1}{K}+\left(1-\frac{1}{K}\right)q.$$
  Stable hashing supplies the Bernoulli draw, so reruns are deterministic. It is not presented as a vision model; it proves the analysis can recover a relationship deliberately inserted into the data.

`bind_cached_labels_for_controls` loads the actual component arrays from the 522-part manifest without loading 50,112 PNGs; its 1×1 placeholder is intentionally restricted to the three scripted controls and must never be passed to a real model. On all 38,864 bound candidate rows, this command:

```text
nice -n 15 env OMP_NUM_THREADS=6 MKL_NUM_THREADS=6 \
  python3 -X utf8 -m services.viewpoint.usefulness --parts 522 \
  --component visibility --manifest scratch/viewpoint-pixels-large/manifest.json
```

measured:

| Answerer | Accuracy | Declared chance | visibility Pearson $r$ | visibility MI (bits) |
|---|---:|---:|---:|---:|
| scripted oracle | 1.000000 | 0.464183 | 0.000000 | 0.000000 |
| blind uniform prior | 0.464980 | 0.464183 | 0.001403 | 0.000226 |
| visibility-degraded | 0.620085 | 0.464183 | 0.461087 | 0.204271 |

The degraded control's full dependence report on those real labels is:

| Inspectability signal | Pearson $r$ | MI (bits) |
|---|---:|---:|
| coverage | 0.001124 | 0.000064 |
| visibility (injected cause) | 0.461087 | 0.204271 |
| foreshortening | 0.018579 | 0.001686 |
| scale | 0.027708 | 0.000625 |
| silhouette | 0.121036 | 0.011636 |
| depth | 0.014695 | 0.000245 |
| uniqueness | 0.138348 | 0.013694 |
| frozen aggregate | 0.295536 | 0.077530 |

Its per-difficulty accuracies are easy 0.695840 (10,192 trials), medium 0.633709 (11,712), and hard 0.565153 (16,960). The nonzero silhouette, uniqueness, and aggregate associations are expected covariance in the real oracle labels; only visibility is causal by construction.

Thus the blind control is within 0.000797 absolute accuracy of chance and is independent of visibility, while the deliberately degraded control recovers its named positive relationship on the real frozen-oracle distribution. Separately, `make_instrument_benchmark` supplies independent deterministic component values so tests can prove that only the named component correlates: the 72-part test slice requires visibility-bin accuracy to rise by more than 0.45 from the lowest to highest eighth, visibility $r>0.33$, visibility MI $>0.09$ bits, and every non-target component $|r|<0.04$. Together these checks prove the measuring instrument can detect a component-to-correctness relationship when one exists without confusing correlation among oracle components for the injected cause.

## Analysis output

`evaluate_answerer` records one result per question/candidate and reports:

- accuracy and declared random-chance accuracy;
- Pearson point-biserial correlation between correctness and every oracle component plus aggregate;
- empirical mutual information between binary correctness and eight fixed inspectability bins;
- count and accuracy in every fixed bin;
- count and accuracy for easy, medium, and hard questions;
- optional chosen-view accuracy for a supplied `question_id -> candidate_id` selection map.

`highest_aggregate_selections` is provided as an explicit oracle-aggregate selection baseline. It is never silently used by evaluation.

## Honest scope

The reported relationship above is an inserted scripted control, not evidence that geometric inspectability predicts a real model's answer correctness. What remains unproven is the central empirical hypothesis: a real image-capable model must be run through `CandidateObservation` on the bound 50,112 renders, after which the same analysis can report whether any component or the frozen aggregate actually predicts useful answers. A weak or absent relationship is a valid result and no threshold in this package is tuned to make the oracle win.

## Live-model probe (2026-08-08) — preliminary, underpowered, and pointed the wrong way

The scripted controls validate the instrument; they cannot answer what the instrument exists for.
This is the first run where a REAL vision model answered the benchmark from real renders.

**Method.** `services/viewpoint/usefulness/trial_pack.py` emits a model-facing pack: one PNG per
trial plus `questions.json` carrying only the `PublicQuestion`. Ground truth and every oracle value
stay in a separate `answers.json` the answering model never reads. Trials are stratified by aggregate
**value** rather than rank, because the candidate distribution is heavily zero-inflated and equal-count
rank bands put nearly every trial at 0.000 — a correlation measured on that sample would report a null
for want of spread rather than for want of a relationship. Pixels and oracle labels are regenerated
together at the viewing resolution (384x384), not upscaled from the 96x96 encoder corpus, because the
oracle's `scale` component is resolution-coupled and at 96x96 these features are barely legible.

**Result, n=18, aggregate spanning 0.000-0.848.** Accuracy 0.722 against a per-trial chance baseline
of 0.472 (13/18, one-sided binomial p=0.029), so the model is genuinely reading the images. But:

| relationship | value |
|---|---:|
| pearson(aggregate inspectability, correct) | **+0.064**, 95 % CI [-0.415, +0.516] |
| pearson(visibility component, correct) | **-0.205** |
| accuracy on low inspectability (agg < 0.2) | 0.833 (n=6) |
| accuracy on high inspectability (agg >= 0.2) | 0.667 (n=12) |

**What this does and does not license.** It does NOT license the conclusion that geometric
inspectability is unrelated to usefulness. The CI on r spans nearly the whole range, and the apparent
inversion is contaminated in an obvious way: the low-inspectability trials are mostly two-choice
questions where a forced guess lands 50 % of the time, and inspection of the per-trial log shows
exactly that — trials 3, 9 and 15 were recorded as guesses and scored correct anyway. Trial 17 is the
honest counter-example in the other direction: the target was entirely out of frame, the oracle scored
it 0.000, and the forced guess was wrong.

What it does license is that the question is live and worth powering properly. A +/-0.15 CI on r needs
roughly **685 trials**, not 18. A properly powered run also needs guess-corrected scoring and questions
with more than two choices, so chance cannot manufacture the inversion seen here.

Reproduce: `python3 -X utf8 -m services.viewpoint.usefulness.trial_pack --trials 24 --parts 40
--resolution 384 --out scratch/viewpoint-live-trials`; answers and per-trial log in
`scratch/viewpoint-live-trials/live_model_results.json`.
