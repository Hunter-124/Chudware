#!/usr/bin/env bash
# ViewNet end-to-end campaign (no LLMs): labels -> Observer sets + strategy sets -> Observer
# cross-fits -> labels -> utility model -> convergence report -> A/B on held-out TEST parts.
# Every stage is resumable (content-addressed renders, per-part done markers, per-fold
# checkpoints, label upserts), so rerunning this script after an interruption continues.
#
#   training/viewpoint/campaign.sh [stage...]     stages: render trials observer (= obs_crossfit
#                         obs_independent obs_report) observe utility round2 ab   (default: all)
# Env: CHUDWARE_VIEWNET_DATA (default ~/.cache/chudware/viewnet), VIEWNET_WORKERS (default 10),
#      VIEWNET_OBS_EPOCHS (default 12). Run it detached (setsid/nohup): it outlives the shell.
# Engine: always a private in-process libchudware — never the user's running hostd.
set -euo pipefail
cd "$(dirname "$0")/../.."
export CHUDWARE_ENGINE_DAEMON=0
export CHUDWARE_LIBCHUDWARE="${CHUDWARE_LIBCHUDWARE:-$PWD/core/build-linux/bin/libchudware.so}"
# Stage output goes through tee: without this, print() is block-buffered and a healthy
# multi-hour stage looks silent for tens of minutes.
export PYTHONUNBUFFERED=1
DATA="${CHUDWARE_VIEWNET_DATA:-$HOME/.cache/chudware/viewnet}"
WORKERS="${VIEWNET_WORKERS:-10}"
LOG="$DATA/campaign/logs"
mkdir -p "$LOG"
PY=python3

stage() {
    local name="$1"; shift
    echo "[campaign] $(date -Is) START $name: $*" | tee -a "$LOG/campaign.log"
    "$@" > >(tee -a "$LOG/$name.log") 2>&1
    echo "[campaign] $(date -Is) DONE  $name" | tee -a "$LOG/campaign.log"
}

run_render()   { stage render   $PY -m training.viewpoint.observer render --workers "$WORKERS"; }
run_trials()   { stage trials   $PY -m training.viewpoint.utility trials --render-workers "$WORKERS"; }
# 12 cosine epochs ≈ 1 h per fold on the RTX 3080 Ti at ~150k sets/fold; the report's learning
# curve says whether more data or epochs would still pay. A fold already on disk (foldN.pt) is
# not retrained, so an interrupted judge resumes at its next fold.
run_judge() {
    local judge="$1" ep="${VIEWNET_OBS_EPOCHS:-12}" dir="$DATA/utility/store/observer/$1" todo=()
    for f in 0 1 2 3 4; do [ -f "$dir/fold$f.pt" ] || todo+=("$f"); done
    if [ ${#todo[@]} -eq 0 ]; then echo "[campaign] $judge: all folds on disk"; return; fi
    local csv; csv=$(IFS=,; echo "${todo[*]}")
    stage "obs_$judge" $PY -m training.viewpoint.observer train --judge "$judge" \
        --epochs "$ep" --folds "$csv"
}
run_obs_crossfit()    { run_judge crossfit; }
run_obs_independent() { run_judge independent; }
# Learning curve over 1/8..1 of the training parts × 2 seeds + controls at 8 epochs ≈ 3 h.
run_obs_report() { stage obs_report $PY -m training.viewpoint.observer report --seeds 0,1 --epochs 8; }
run_observer() { run_obs_crossfit; run_obs_independent; run_obs_report; }
run_observe() {
    stage observe_crossfit    $PY -m training.viewpoint.utility observe --judge crossfit
    stage observe_independent $PY -m training.viewpoint.utility observe --judge independent --splits val
}
run_utility() {
    stage util_train    $PY -m training.viewpoint.utility train
    stage util_evaluate $PY -m training.viewpoint.utility evaluate
}
run_round2() {
    stage trials_r2 $PY -m training.viewpoint.utility trials --render-workers "$WORKERS" \
        --weights "$DATA/utility/run/utility_v1.npz" --splits train,val
    run_observe
    run_utility
}
run_ab() { stage ab $PY -m training.viewpoint.utility ab --render-workers "$WORKERS"; }

stages=("$@")
[ ${#stages[@]} -eq 0 ] && stages=(render trials observer observe utility round2 ab)
for s in "${stages[@]}"; do "run_$s"; done
