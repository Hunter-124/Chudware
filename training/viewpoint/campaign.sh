#!/usr/bin/env bash
# ViewNet end-to-end campaign (no LLMs): labels -> Observer sets + strategy sets -> Observer
# cross-fits -> labels -> utility model -> convergence report -> A/B on held-out TEST parts.
# Every stage is resumable (content-addressed renders, per-part done markers, per-fold
# checkpoints, label upserts), so rerunning this script after an interruption continues.
#
#   training/viewpoint/campaign.sh [stage...]     stages: render trials observer (= obs_crossfit
#                         obs_independent obs_report) observe utility round2 ab   (default: all)
# Env: CHUDWARE_VIEWNET_DATA (default ~/.cache/chudware/viewnet), VIEWNET_WORKERS (default 10),
#      VIEWNET_OBS_EPOCHS (default 12), VIEWNET_RUN (done-marker set, default "current").
# Launch as a reboot-surviving user service (flaky GPU hardware can hard-reset the box):
#      training/viewpoint/campaign.sh service [stage...]
# It resumes after any crash/reboot and disables itself when the stage list completes.
# Stop: systemctl --user disable --now viewnet-campaign.service
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

# Done markers make a rerun skip completed stages. They are keyed by run and invocation order,
# so round2's second observe/utility pass is distinct from the first.
RUN="${VIEWNET_RUN:-current}"
DONE="$DATA/campaign/runs/$RUN/done"
mkdir -p "$DONE"
SEQ=0
stage() {
    local name="$1"; shift
    SEQ=$((SEQ + 1))
    local marker; marker="$DONE/$(printf %02d "$SEQ")_$name"
    if [ -f "$marker" ]; then echo "[campaign] skip $name (done $(cat "$marker"))"; return; fi
    echo "[campaign] $(date -Is) START $name: $*" | tee -a "$LOG/campaign.log"
    "$@" > >(tee -a "$LOG/$name.log") 2>&1
    date -Is > "$marker"
    echo "[campaign] $(date -Is) DONE  $name" | tee -a "$LOG/campaign.log"
}

run_render()   { stage render   $PY -m training.viewpoint.observer render --workers "$WORKERS"; }
run_trials()   { stage trials   $PY -m training.viewpoint.utility trials --render-workers "$WORKERS"; }
run_dense()    { stage dense    $PY -m training.viewpoint.observer dense --workers "$WORKERS"; }
# 12 cosine epochs ≈ 1 h per fold on the RTX 3080 Ti at ~150k sets/fold; the report's learning
# curve says whether more data or epochs would still pay. A fold already on disk (foldN.pt
# recorded in folds.json under the current observer/query schema) is not retrained, so an
# interrupted judge resumes at its next fold; stale-schema folds are retrained.
run_judge() {
    local judge="$1" ep="${VIEWNET_OBS_EPOCHS:-12}" todo=()
    mapfile -t todo < <($PY - "$judge" "$DATA/utility/store" <<'EOF'
import json, sys
from training.viewpoint.observer.train import OBSERVER_SCHEMA, QUERY_SCHEMA, folds_meta_path
judge, store = sys.argv[1:]
meta_path = folds_meta_path(store, judge)
meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
ok = meta.get("schema") == OBSERVER_SCHEMA and meta.get("query_schema") == QUERY_SCHEMA
for f in range(5):
    if not (ok and str(f) in meta.get("folds", {}) and (meta_path.parent / f"fold{f}.pt").exists()):
        print(f)
EOF
)
    # Still goes through stage() so the done-marker sequence is identical on every rerun.
    if [ ${#todo[@]} -eq 0 ]; then stage "obs_$judge" echo "$judge: all folds on disk"; return; fi
    local csv; csv=$(IFS=,; echo "${todo[*]}")
    stage "obs_$judge" $PY -m training.viewpoint.observer train --judge "$judge" \
        --epochs "$ep" --folds "$csv" --workers "$WORKERS"
}
run_obs_crossfit()    { run_judge crossfit; }
run_obs_independent() { run_judge independent; }
# Learning curve over 1/8..1 of the training parts × 2 seeds + controls at 8 epochs ≈ 3 h.
run_obs_report() { stage obs_report $PY -m training.viewpoint.observer report --seeds 0,1 --epochs 8 --workers "$WORKERS"; }
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
[ ${#stages[@]} -eq 0 ] && stages=(render trials dense observer observe utility round2 ab)

# `campaign.sh service [stage...]` installs a persistent user unit (lingering user manager) so
# a crash or reboot resumes the campaign: completed stages are skipped via their done markers,
# the interrupted stage re-enters (every stage is resumable), and the unit disables itself when
# the whole list finishes. Starting a new campaign = new VIEWNET_RUN (fresh marker set).
if [ "${stages[0]}" = service ]; then
    stages=("${stages[@]:1}")
    [ ${#stages[@]} -eq 0 ] && stages=(render trials dense observer observe utility round2 ab)
    unit="$HOME/.config/systemd/user/viewnet-campaign.service"
    mkdir -p "$(dirname "$unit")"
    cat > "$unit" <<EOF
[Unit]
Description=ViewNet campaign ($RUN: ${stages[*]})
StartLimitIntervalSec=6h
StartLimitBurst=6

[Service]
Type=simple
WorkingDirectory=$PWD
Environment=HOME=$HOME PATH=$PATH VIEWNET_RUN=$RUN VIEWNET_SERVICE=1 VIEWNET_WORKERS=$WORKERS
Environment=CHUDWARE_VIEWNET_DATA=$DATA VIEWNET_OBS_EPOCHS=${VIEWNET_OBS_EPOCHS:-12}
# After a reboot the user manager can start before the NVIDIA driver is usable.
ExecStartPre=/bin/sh -c 'for i in \$(seq 60); do nvidia-smi >/dev/null 2>&1 && exit 0; sleep 5; done; exit 0'
ExecStart=$PWD/training/viewpoint/campaign.sh ${stages[*]}
StandardOutput=append:$LOG/driver.out
StandardError=append:$LOG/driver.out
MemoryMax=24G
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now viewnet-campaign.service
    echo "[campaign] installed $unit (run $RUN): ${stages[*]}"
    exit 0
fi

for s in "${stages[@]}"; do "run_$s"; done
echo "[campaign] $(date -Is) RUN $RUN COMPLETE: ${stages[*]}" | tee -a "$LOG/campaign.log"
if [ "${VIEWNET_SERVICE:-0}" = 1 ]; then systemctl --user disable viewnet-campaign.service || true; fi
