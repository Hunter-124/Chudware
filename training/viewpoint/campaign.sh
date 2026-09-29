#!/usr/bin/env bash
# ViewNet end-to-end campaign (no LLMs): labels -> Observer sets + strategy sets -> Observer
# cross-fits -> labels -> utility model -> convergence report -> A/B on held-out TEST parts.
# Every stage is resumable (content-addressed renders, per-part done markers, per-fold
# checkpoints, label upserts), so rerunning this script after an interruption continues.
#
#   training/viewpoint/campaign.sh [stage...]     stages: render trials observer observe
#                                                          utility round2 ab   (default: all)
# Env: CHUDWARE_VIEWNET_DATA (default ~/.cache/chudware/viewnet), VIEWNET_WORKERS (default 10).
# Engine: always a private in-process libchudware — never the user's running hostd.
set -euo pipefail
cd "$(dirname "$0")/../.."
export CHUDWARE_ENGINE_DAEMON=0
export CHUDWARE_LIBCHUDWARE="${CHUDWARE_LIBCHUDWARE:-$PWD/core/build-linux/bin/libchudware.so}"
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
run_observer() {
    stage obs_crossfit    $PY -m training.viewpoint.observer train --judge crossfit
    stage obs_independent $PY -m training.viewpoint.observer train --judge independent
    stage obs_report      $PY -m training.viewpoint.observer report
}
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
