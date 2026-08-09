# Perception Model: Forecast-to-Compress Evaluation Protocol

## Status and task definition

The Manufacturing Latent Dynamics Predictor (MLDP) is a bounded **advisory-only monitoring component**, not the project's headline contribution. Given a fixed history of camera and process features plus the already-planned future command stream, it predicts the future camera latent state at several horizons and a Gaussian variance for each prediction. Observed departures from that forecast produce a surprise score used to recommend event review and to rank intervals for retention. MLDP does not authorize motion, stop a machine, validate guards, or provide any safety function. A controller or safety PLC must not depend on its output.

The publication task is fixed-seed supervised prediction on a deterministic synthetic corpus. The default CPU qualification uses seed `1729`, 384 windows, a 60/20/20 seeded split, and horizons 1, 2, and 4 ticks on a 4 Hz grid. The locked test partition is read only after model and baseline fitting and event-threshold calibration.

## Monitoring role: forecast-to-compress

Within the monitoring lane, forecast-to-compress asks which manufacturing observations were predictable from prior observations, process telemetry, and the commanded future. Predictable intervals can receive lower retention priority; high residual likelihood, event boundaries, and uncertain intervals receive higher priority. This joins perception and process intent without claiming that an anomaly score is a physical hazard detector. The released reference is deliberately small and CPU-reproducible; it exercises the data, training, evaluation, and publication contracts, but its failed variance-calibration bar precludes a production-accuracy claim.

## Tensor contract

`ModelSpec` is the authoritative dimension record and its canonical JSON SHA-256 is `feature_spec_id`. `B` is batch size, `C` cameras, `T` context ticks, `Fcam` camera-feature width, `Fproc` process-feature width, `Fcmd` command width, `K` forecast horizons, and `Dz` latent width.

| Tensor | Shape | Type / meaning |
|---|---:|---|
| `cam_feat` | `[B,C,T,Fcam]` | float32 causal camera features |
| `cam_mask` | `[B,C,T]` | binary camera availability |
| `cam_age` | `[B,C,T]` | finite age/staleness feature |
| `proc_feat` | `[B,T,Fproc]` | float32 process features |
| `proc_mask` | `[B,T,Fproc]` | binary process-feature availability |
| `proc_age` | `[B,T,Fproc]` | finite age/staleness feature |
| `cmd_future` | `[B,Hmax,Fcmd]` | commanded future, not measured future |
| `cmd_mask` | `[B,Hmax]` | binary command availability |
| `target_latent` | `[B,K,C,Dz]` | observed future latent target |
| `event_label` | `[B]` | whether any labelled deviation intersects a target tick |
| `z_hat` | `[B,K,C,Dz]` | predicted latent mean |
| `z_logvar` | `[B,K,C,Dz]` | predicted log variance, clipped to a finite range |

The CPU reference defaults are `C=2`, `T=8`, `Fcam=6`, `Fproc=5`, `Fcmd=3`, `Dz=4`, hidden width 12, and horizons `(1,2,4)`. `ModelSpec.production()` records the intended larger envelope: four cameras, 64 context ticks, camera/process/command widths 200/64/16, latent width 128, hidden width 256, and horizons `(1,4,20,120)`. The publication run evaluates the CPU reference defaults only.

## Dataset and manifest schema

Every source clip is represented by one strict `ManifestRecord`; unknown and missing fields are rejected. Records cover:

- asset identity and integrity: `clip_id`, `uri`, `sha256`, bytes, codec, resolution, frame rates, and duration;
- manufacturing provenance: device, machine serial, controller, run/job/revision, artifact/program hashes, material, tools, fixture, WCS, and process family;
- camera and calibration provenance: media/camera/station IDs, serial, intrinsics/extrinsics, calibration dates, mount, lighting, and exposure;
- time alignment: secure transport, UTC/tick origin, clock source/error, mandatory 4 Hz grid, alignment method, and controller/sensor/program sidecars with hashes;
- rights and privacy: `license`, `license_ref`, `consent_ref`, hashed operator ID, `redaction_applied`, retention class, and `export_restricted`;
- annotation: pass, annotator IDs, double-annotation/adjudication flags, IAA kappa, and label-schema version;
- synthetic provenance: synthetic flag/source, twin snapshot/frame hash, and appearance domain;
- leakage control: `split_group`, split, and lock time. A split group is SHA-1 of machine serial, job ID, and calendar day and may occur in only one split.

Allowed licenses are `owned`, `licensed`, `synthetic`, and `third_party`; validation alone does **not** grant publication rights. Synthetic records must use a synthetic license/source and may enter training only. Locked test data is real-only.

### Publication eligibility predicate

A record enters a published dataset subset only if all of the following are true:

1. it passes `ManifestRecord.from_dict`;
2. `export_restricted == false` and `redaction_applied == true`;
3. `license` is `owned`, `licensed`, or `synthetic` (`third_party` is excluded by default);
4. `license_ref` belongs to the release review's explicit approved-license set and that review confirms public redistribution;
5. `consent_ref` belongs to the release review's explicit approved-consent set and that review confirms model training and public redistribution.

`publication_eligible()` enforces the machine-checkable form by requiring both approval sets. Non-empty reference strings are never treated as approval. A synthetic no-personal-data declaration still needs an approved consent reference. Reclassifying third-party material requires a future schema carrying an explicit redistribution grant; it is not an operator override.

## Splits and fitting

The fixed-seed permutation reserves the final 20% of episodes as locked test and never exposes it to fitting or selection. The remaining 80% is the development/train portion. The fast default remains seed `1729` with 384 total windows: 307 development windows, comprising 230 network-fit and 77 final checkpoint/calibration examples, plus 77 locked-test examples. The opt-in sample-size ladder uses 1,250 and 3,750 total windows to obtain exactly 1,000 and 3,000 development windows while preserving the same 60/20/20 split. Variance-method selection is five-fold cross-validation over the complete development portion at every size. Training is deterministic and CPU-only: 800 full-batch AdamW steps, learning rate `0.01`, weight decay `1e-5`, and gradient norm clipping at 5.

The primary objective is heteroscedastic Gaussian NLL over every horizon, camera, and latent component with weight `1.0`. The auxiliary three-channel event-boundary/precursor BCE has weight `0.2` and training-split positive-class weighting capped at 64. NLL has unit weight because forecasting and conditional variance are the model's primary contract; `0.2` keeps the sparse precursor task strong enough to shape the shared encoder without allowing it to dominate forecasting. The mean head, conditional log-variance head, camera/process projections, dilated temporal layers, command projection, horizon embeddings, and event head all receive gradients. The event target marks precursor onset, precursor extent, and its window-level continuation. The MLDP event score is the maximum precursor probability in the context, thresholded at the higher-method 0.99 quantile of non-event calibration scores. Baselines retain the same quantile protocol over their surprise scores. No threshold or checkpoint is selected on test data.

The synthetic corpus consists of independent episodes in three manufacturing regime families: steady cut, an in-window material transition, and a tool-wear ramp. State transitions combine planned commands, inertia, velocity, material response, and wear. A delayed event has a process-channel precursor at ticks `t0-3:t0-2`, which is absent from the current frame and affects only future targets. Camera/process noise, camera/process dropouts, occasional current-camera loss, and planned-command dropout are explicit. Train, calibration, and locked-test episodes draw regime parameters from close but disjoint continuous bands, so a held-out trajectory cannot be memorised from train. The published `synthetic_corpus.npz` carries the inputs, targets, labels, IDs, regime/split labels, seed, and spec. A causality regression perturbs every camera and process input after an interior tick and requires the encoded prefix to remain bit-identical while the perturbed suffix changes.

## Baselines

All baselines receive the same train/calibration/test windows and use the same residual-variance and event-threshold protocol:

- **Persistence:** every horizon equals the latest observed camera latent.
- **Linear autoregressor:** ridge regression from the latest camera latent plus the command at the evaluated horizon, with an intercept, fitted independently per horizon.
- **Mean predictor:** the training-set mean target per horizon and latent feature, shared across cameras.

Results are reported without winner filtering. A baseline win remains in `metrics.json`; the reproduction script has no pass criterion requiring MLDP to win.

## Metrics

For horizon $k$ over every test batch, camera, and latent component:

- MAE: $\operatorname{mean}(|z-\hat z|)$.
- RMSE: $\sqrt{\operatorname{mean}((z-\hat z)^2)}$.

With predicted variance $\sigma^2=\exp(\widehat{\log\sigma^2})$:

- standardized MSE: $\operatorname{mean}((z-\hat z)^2/\sigma^2)$;
- variance calibration error: absolute distance of standardized MSE from 1;
- one- and two-sigma coverage: empirical fractions with $|z-\hat z|\leq\sigma$ and $|z-\hat z|\leq2\sigma$;
- coverage errors: absolute distances from Gaussian nominal coverage 0.682689492 and 0.954499736.

Per-window surprise is the mean heteroscedastic Gaussian negative log-likelihood term $\tfrac12((z-\hat z)^2/\sigma^2+\log\sigma^2)$. Baseline event detection thresholds this surprise. MLDP event detection instead thresholds the learned precursor probability described above, so its auxiliary head is evaluated rather than trained and discarded. Precision, recall, and F1 use ordinary TP/FP/FN definitions; zero-denominator precision or recall is reported as zero. Confusion counts, mean surprise, and measured variance calibration are also published.

## Corpus diagnosis and honest benchmark verdict

The original seed-1729/384-window run was captured before changing the generator. Its inputs looked correlated, but that did not become held-out target predictability. Lag-1 autocorrelations by channel were:

| Input family | Per-channel lag-1 autocorrelation |
|---|---|
| camera 0..5 | `0.0018, -0.0023, 0.5758, 0.6402, -0.0066, 0.0021` |
| process 0..4 | `0.7638, 0.7233, 0.8697, 0.0128, -0.0220` |
| planned command 0..2 | `0.9049, 0.8856, 0.9390` |

The maximum absolute command/future-target correlation proxy was `0.5455/0.5444/0.5369` at horizons 1/2/4, and the context/future-target proxy was `0.7916/0.7398/0.6567`. Those pairwise correlations were misleading: on the locked split, variance explained by a training mean (constant), a fitted lag-1 state probe, and that probe plus the horizon command was:

| Original corpus | constant $R^2$ | lag-1 $R^2$ | command-conditioned $R^2$ |
|---|---:|---:|---:|
| horizon 1 | 0.0000 | -0.0074 | -0.0240 |
| horizon 2 | 0.0000 | 0.0005 | -0.0325 |
| horizon 4 | 0.0000 | -0.0039 | -0.0192 |
| overall | 0.0000 | -0.0026 | -0.0271 |

**Diagnosis:** the original benchmark target did not contain useful held-out dynamics. Smooth normal motion existed, but large future latent deviations were injected without any precursor in the context, dominated squared error, and were therefore unpredictable. The mean predictor's win was a dataset failure, not evidence that temporal modelling was unnecessary.

Unfiltered original results (MAE/RMSE pairs, variance calibration error, event F1):

| Original predictor | h1 | h2 | h4 | variance calibration error | coverage 1σ / 2σ | event F1 |
|---|---:|---:|---:|---:|---:|---:|
| MLDP | 0.1795 / 0.6503 | 0.2741 / 0.9222 | 0.1898 / 0.6709 | 0.0135 | 0.8198 / 0.9432 | 0.9412 |
| persistence | 0.2207 / 0.9002 | 0.3211 / 1.1027 | 0.2312 / 0.9004 | 0.1842 | 0.7873 / 0.9232 | 0.0000 |
| linear autoregressor | 0.1761 / 0.6440 | 0.2623 / 0.9183 | 0.1516 / 0.6437 | 0.0101 | 0.8203 / 0.9437 | 0.9697 |
| mean | 0.1351 / 0.6364 | 0.2359 / 0.9037 | 0.1385 / 0.6376 | 0.0224 | 0.8139 / 0.9443 | 0.9412 |

After replacing the corpus physics, per-channel lag-1 autocorrelations are camera `0.8636, 0.8594, 0.8660, 0.8814, 0.0451, 0.0021`, process `0.7786, 0.7886, 0.7754, 0.8939, 0.4421`, and planned command `0.7771, 0.7535, 0.7757`. The command/future-target correlation proxy is `0.8676/0.7326/0.8665` and the context/future-target proxy is `0.9383/0.8327/0.6070`. The held-out probes now explain:

| Improved corpus | constant $R^2$ | lag-1 $R^2$ | command-conditioned $R^2$ |
|---|---:|---:|---:|
| horizon 1 | 0.0000 | 0.8023 | 0.8579 |
| horizon 2 | 0.0000 | 0.6153 | 0.7518 |
| horizon 4 | 0.0000 | 0.3099 | 0.8319 |
| overall | 0.0000 | 0.5831 | 0.8131 |

The improved corpus therefore does contain strong temporal and command-conditioned dynamics. At every operating size, the locked test partition remains untouched until every model fit, calibration fit, and variance-method selection decision is complete.

Variance-method selection uses deterministic, regime/event-stratified five-fold cross-validation. For each outer fold, the other four folds are deterministically divided 80/20 into network-fit and inner checkpoint/calibration data; neither the network fit nor variance calibration sees the outer fold it scores. Five folds make every development window an out-of-fold observation, retain all three regimes in each fit, and keep the seeded CPU sweep bounded. The training objective, fold seed, split rules, candidate fits, and selection rule are identical at every sample size.

The five candidates are β-NLL with β=`0.5`, two-stage mean-then-variance fitting, scalar variance temperature, per-horizon variance temperatures, and affine log-variance plus nominal-2σ conformal temperature. A statistical tie is defined from paired fold differences: starting from the lowest mean OOF NLL, candidate $j$ is tied when its mean paired difference is no larger than $\operatorname{SE}(\Delta_j)=s_{\Delta_j}/\sqrt{5}$. Parsimony among tied methods uses fitted calibration degrees of freedom: β-NLL/two-stage `0`, scalar `1`, per-horizon `3`, and affine-conformal `14`. Fold wins are raw per-fold NLL argmins. “Stable” requires the selected method to win all five folds.

The completed seed-1729 sweep is below. `tie` means statistically tied with the raw OOF-NLL best; the raw best is therefore `yes` by definition. Locked-test calibration error was computed only after that row's selection was complete.

| development (`total`; locked test) | variance method | mean OOF NLL ± SE | fold wins | tie | locked-test variance calibration error |
|---|---|---:|---:|---:|---:|
| 307 (`384`; 77) | scalar temperature **(selected)** | -1.502665 ± 0.039072 | 3/5 | yes | 0.036435 |
| 307 (`384`; 77) | affine + 2σ conformal temperature | -1.498993 ± 0.039153 | 2/5 | yes | 0.001440 |
| 307 (`384`; 77) | per-horizon temperatures | -1.495883 ± 0.040189 | 0/5 | no | 0.016466 |
| 307 (`384`; 77) | two-stage mean then variance | -1.195804 ± 0.036269 | 0/5 | no | 0.106806 |
| 307 (`384`; 77) | β-NLL, β=0.5 | -1.027792 ± 0.160014 | 0/5 | no | 0.913413 |
| 1,000 (`1,250`; 250) | scalar temperature **(selected)** | -1.847073 ± 0.085393 | 4/5 | yes | 0.138754 |
| 1,000 (`1,250`; 250) | per-horizon temperatures | -1.844363 ± 0.086310 | 0/5 | no | 0.137234 |
| 1,000 (`1,250`; 250) | affine + 2σ conformal temperature | -1.841949 ± 0.089108 | 0/5 | no | 0.111145 |
| 1,000 (`1,250`; 250) | β-NLL, β=0.5 | -1.684432 ± 0.149912 | 1/5 | no | 0.198289 |
| 1,000 (`1,250`; 250) | two-stage mean then variance | -1.523976 ± 0.069997 | 0/5 | no | 0.160857 |
| 3,000 (`3,750`; 750) | scalar temperature **(selected)** | -2.226204 ± 0.007927 | 3/5 | yes | 0.112627 |
| 3,000 (`3,750`; 750) | per-horizon temperatures | -2.226057 ± 0.007869 | 0/5 | yes | 0.115475 |
| 3,000 (`3,750`; 750) | affine + 2σ conformal temperature | -2.226038 ± 0.007747 | 2/5 | yes | 0.090630 |
| 3,000 (`3,750`; 750) | β-NLL, β=0.5 | -2.187335 ± 0.005712 | 0/5 | no | 0.260538 |
| 3,000 (`3,750`; 750) | two-stage mean then variance | -1.912298 ± 0.036810 | 0/5 | no | 0.212412 |

The size-level decisions are:

| development windows | raw top pair | paired mean difference | paired SE | top pair tied? | fold winners | stable? |
|---:|---|---:|---:|---:|---|---:|
| 307 | scalar / affine-conformal | 0.003672719 | 0.004458612 | yes | scalar 3, affine 2 | no |
| 1,000 | scalar / per-horizon | 0.002710056 | 0.001056690 | no | scalar 4, β-NLL 1 | no |
| 3,000 | scalar / per-horizon | 0.000146723 | 0.000151973 | yes | scalar 3, affine 2 | no |

**No completed size is both decisive and stable.** At 1,000 development windows the top-pair difference exceeds its paired SE, but the fold winner is still unstable. At 3,000, the top pair is tied again and the 3/2 fold split remains unstable. Thus there is no defensible sample-size threshold in reach. The 10,000-development-window point exceeded the bounded ten-minute CPU run and produced no result, so it is not represented as measured evidence.

The fast default therefore remains 384 total / 307 development windows. Its measured wall time was 89.8 seconds on the qualification CPU, while 1,000 and 3,000 development windows took 183.4 and 416.1 seconds. Promoting the larger run would make the routine command slower without satisfying the decisive-and-stable criterion. The three-point ladder is an explicit `reproduce sweep` opt-in. The default still selects scalar temperature through the one-SE parsimony tie-break; the locked test is never used to choose that default.

After default-size selection was locked, every final candidate was fitted on the same 230/77 fit/calibration partition and measured on the original 77-window locked test. Reporting every candidate in the sweep table exposes the selection-to-test gap: affine-conformal has test calibration error `0.001440` versus selected scalar's `0.036435`, but that is evaluation evidence, not permission to select on test.

The complete default-size locked-test result for all four predictors is:

| Predictor | h1 MAE / RMSE | h2 MAE / RMSE | h4 MAE / RMSE | variance calibration error | coverage 1σ / 2σ | event F1 |
|---|---:|---:|---:|---:|---:|---:|
| MLDP | 0.0816 / 0.1129 | 0.1058 / 0.1401 | 0.1494 / 0.1928 | 0.0364 | 0.6943 / 0.9583 | 1.0000 |
| persistence | 0.1437 / 0.2362 | 0.2354 / 0.3356 | 0.3605 / 0.4534 | 0.0747 | 0.7256 / 0.9367 | 0.5714 |
| linear autoregressor | 0.1111 / 0.1978 | 0.1582 / 0.2635 | 0.1557 / 0.2066 | 0.0040 | 0.7808 / 0.9459 | 0.8889 |
| mean | 0.4159 / 0.5248 | 0.4111 / 0.5290 | 0.4082 / 0.5040 | 0.2626 | 0.6245 / 0.9248 | 0.3333 |

Per-regime default-size locked-test results prevent the aggregate comparison from hiding weak regimes:

| Predictor / regime (`n`) | h1 MAE / RMSE | h2 MAE / RMSE | h4 MAE / RMSE | variance calibration error | coverage 1σ / 2σ | event F1 |
|---|---:|---:|---:|---:|---:|---:|
| MLDP / material change (26) | 0.0963 / 0.1320 | 0.1288 / 0.1672 | 0.1838 / 0.2343 | 0.2530 | 0.6603 / 0.9231 | 1.0000 |
| MLDP / steady cut (26) | 0.0632 / 0.0839 | 0.0720 / 0.0906 | 0.1108 / 0.1404 | 0.2513 | 0.7372 / 0.9824 | 1.0000 |
| MLDP / wear ramp (25) | 0.0853 / 0.1175 | 0.1171 / 0.1510 | 0.1537 / 0.1919 | 0.1140 | 0.6850 / 0.9700 | 1.0000 |
| persistence / material change (26) | 0.1550 / 0.2594 | 0.2594 / 0.3724 | 0.3915 / 0.4829 | 0.2511 | 0.6955 / 0.9151 | 0.6667 |
| persistence / steady cut (26) | 0.1367 / 0.2297 | 0.2277 / 0.3226 | 0.3124 / 0.4144 | 0.0683 | 0.7692 / 0.9535 | 0.6667 |
| persistence / wear ramp (25) | 0.1392 / 0.2168 | 0.2185 / 0.3072 | 0.3784 / 0.4604 | 0.0399 | 0.7117 / 0.9417 | 0.0000 |
| linear autoregressor / material change (26) | 0.1130 / 0.2159 | 0.1648 / 0.2877 | 0.1762 / 0.2268 | 0.1265 | 0.7756 / 0.9391 | 1.0000 |
| linear autoregressor / steady cut (26) | 0.1033 / 0.1860 | 0.1373 / 0.2431 | 0.1357 / 0.1991 | 0.1777 | 0.8157 / 0.9535 | 1.0000 |
| linear autoregressor / wear ramp (25) | 0.1172 / 0.1899 | 0.1732 / 0.2576 | 0.1551 / 0.1918 | 0.0656 | 0.7500 / 0.9450 | 0.0000 |
| mean / material change (26) | 0.4928 / 0.6161 | 0.4831 / 0.6127 | 0.4648 / 0.5731 | 0.6990 | 0.5497 / 0.8622 | 0.2857 |
| mean / steady cut (26) | 0.3447 / 0.4339 | 0.3470 / 0.4473 | 0.3459 / 0.4322 | 0.1183 | 0.7019 / 0.9776 | 0.6667 |
| mean / wear ramp (25) | 0.4101 / 0.5077 | 0.4030 / 0.5131 | 0.4142 / 0.4965 | 0.2048 | 0.6217 / 0.9350 | 0.0000 |

For comparability, the new largest completed 750-window locked test at 3,000 development windows is reported separately:

| Predictor | h1 MAE / RMSE | h2 MAE / RMSE | h4 MAE / RMSE | variance calibration error | coverage 1σ / 2σ | event F1 |
|---|---:|---:|---:|---:|---:|---:|
| MLDP | 0.0479 / 0.0713 | 0.0592 / 0.0842 | 0.0796 / 0.1085 | 0.1126 | 0.6698 / 0.9430 | 0.9421 |
| persistence | 0.1186 / 0.2054 | 0.1714 / 0.2968 | 0.2060 / 0.2700 | 0.1396 | 0.7427 / 0.9329 | 0.8550 |
| linear autoregressor | 0.0978 / 0.1757 | 0.1349 / 0.2551 | 0.1101 / 0.1619 | 0.0944 | 0.7870 / 0.9339 | 0.8960 |
| mean | 0.2389 / 0.3175 | 0.2566 / 0.3643 | 0.2397 / 0.3130 | 0.1525 | 0.6793 / 0.9320 | 0.7934 |

The final default joint checkpoint remains step 187. Scalar calibration changes only `logvar_b`; mean forecasts and precursor-event parameters are unchanged, so MAE/RMSE and event F1 match the previous fit. The calibrated conditional `logvar_w` L2 norm is `7.0252`, so the export remains state-dependent rather than collapsing to constant variance.

The scalar transformation is absorbed into the existing `logvar_b` array, so the frozen NumPy forward graph and tensor shapes do not change. The largest observed default NumPy-versus-Torch difference across every exported head is `2.2649765e-6`, inside the asserted absolute tolerance `1e-5`; `services/perception/reference.py` and its contract test remain unchanged.

**Verdict at the default size:** the MLDP does **not** meet the complete bar. It beats the linear autoregressor on every horizon MAE/RMSE and improves event F1 from `0.8889` to `1.0000`, but its locked-test variance calibration error is `0.0364347` versus `0.0039980`. Selection is tied and unstable.

**Verdict at the largest completed size:** the MLDP still does **not** meet the complete bar. At 3,000 development windows it beats the linear autoregressor on every horizon MAE/RMSE and improves event F1 from `0.8960` to `0.9421`, but variance calibration error is `0.1126273` versus `0.0943931`. More data improves forecast error but neither makes calibration selection decisive and stable nor clears the calibration bar. There is no “only with more data” win in the measured ladder.

## Exact reproduction

From the repository root, with NumPy and Torch available and no network, GPU, or hardware device:

```bash
python3 -X utf8 -m training.perception.reproduce run --out scratch/perception-publication
python3 -X utf8 -m training.perception.reproduce verify --reference scratch/perception-publication
python3 -X utf8 -m pytest training/perception services/perception -q
```

The routine `run` remains the 384-window default. The measured three-point sweep is deliberately opt-in because its 3,000-development endpoint takes about seven minutes:

```bash
python3 -X utf8 -m training.perception.reproduce sweep \
  --out scratch/perception-sweep
```

It writes `sample_size_sweep.json`, including every candidate row, selected-method/fold-stability metadata, aggregate and per-regime predictor metrics, and the machine verdict at each size. `--development-sizes` can override the default `307 1000 3000` ladder without changing the five-fold protocol.

The run writes exactly:

- `mldp_reference.npz`: checkpoint tensors plus canonical model metadata;
- `synthetic_corpus.npz`: generated inputs, targets, labels, IDs, regime/split labels, seed, and spec;
- `metrics.json`: split sizes, structure diagnostics, unfiltered aggregate/per-regime MLDP and baseline results, and the machine-readable verdict;
- `publication_manifest.json`: exact release-file names and SHA-256 values for artifacts, training-core sources, and this protocol.

Default verification requires byte-identical `metrics.json` and bit-identical checkpoint/corpus arrays. ZIP container bytes are not compared because NPZ member timestamps are packaging metadata. For a different CPU/BLAS stack, the documented portable check is:

```bash
python3 -X utf8 -m training.perception.reproduce verify \
  --reference scratch/perception-publication --atol 1e-7
```

That mode requires identical structure and nonnumeric values, and compares every numeric metric leaf and floating archive array with absolute tolerance $10^{-7}$ and `rtol=0`. It must be named as tolerance verification, never bit reproduction.

## Publication manifest and hardware envelope

`publication_manifest.json` is generated only after the corpus, checkpoint, and metrics exist. Its `release_files` is the complete bundle; `training_core` is the precise source set needed to rebuild it, including the NumPy runtime model in `services/perception` (which the publication workflow consumes but does not modify). Each listed source and non-self-referential artifact carries SHA-256. The manifest names itself in the release set but cannot include its own digest without a circular definition.

Hardware envelopes:

- **RTX 3080 Ti training:** intended production-scale training envelope; this CPU publication script neither requires nor benchmarks it.
- **RTX 2080 inference:** intended discrete-GPU inference floor for a production export; no latency or throughput claim is made by this protocol.
- **CPU smoke/reproduction:** required and exercised path for the small NumPy reference; no GPU, network, controller, camera, or machine is used.

## Limitations

The corpus is synthetic, tiny, and structurally easier than a real factory. It includes simple material and wear regimes, noise, and random dropouts, but does not cover real optics, lighting changes, complex occlusion, controller timing errors, human activity, adversarial inputs, or a factory's long-tail failure distribution. The locked split uses unseen synthetic regime realizations, not real-machine domain shift. Calibration is marginal rather than a safety guarantee, and the measured overconfidence fails the stated acceptance bar. Event labels indicate injected delayed forecast deviations, not hazards or root causes. Production dimensions, ONNX/GPU performance, RTX 3080 Ti training, RTX 2080 inference, and any hardware integration remain unmeasured.
