#!/usr/bin/env bash
# Shared operator lifecycle; source after selecting the protected gateway config.
# fd 8 carries only the private lease, never a session; fd 9 remains the deploy lock.
set +x
DRAIN_RELEASE_UNCONFIRMED=0
DRAIN_ACQUIRED=0
DRAIN_BORROWED=0
DRAIN_HELPER="$(dirname "${BASH_SOURCE[0]}")/gateway_preflight.py"
if [[ -n "${CHUDWARE_DEPLOY_DRAIN_FD:-}" ]]; then
  [[ "$CHUDWARE_DEPLOY_DRAIN_FD" = 8 ]] || return 4
  DRAIN_ACQUIRED=1
  [[ "${CHUDWARE_DEPLOY_DRAIN_OWNER_PID:-}" = "$$" ]] || DRAIN_BORROWED=1
fi

deployment_drain_call() {
  local action="$1"
  local args=(deployment-drain "$action" --env-file "$GATEWAY_ENV" --environment "$ENVIRONMENT"
    --lease-fd 8 --port "${CHUDWARE_WEB_GATEWAY_PORT:-$GATEWAY_PORT}"
    --max-wait "${CHUDWARE_WEBD_RESTART_MAX_WAIT:-600}")
  if [[ -n "${CHUDWARE_DEPLOY_SESSION_FD:-}" ]]; then
    [[ "$CHUDWARE_DEPLOY_SESSION_FD" =~ ^[0-9]+$ ]] || return 4
    args+=(--session-fd "$CHUDWARE_DEPLOY_SESSION_FD")
  fi
  "$GATEWAY_PY" -I "$DRAIN_HELPER" "${args[@]}"
}

deployment_drain_acquire() {
  if [[ "$DRAIN_ACQUIRED" = 1 ]]; then
    deployment_drain_call check
    return
  fi
  local private_file
  private_file="$(mktemp)" || return 4
  chmod 600 "$private_file" || { rm -f "$private_file"; return 4; }
  if ! /usr/bin/python3 -I -S -c 'import secrets; print(secrets.token_hex(32))' > "$private_file"; then
    rm -f "$private_file"
    return 4
  fi
  exec 8<"$private_file" || { rm -f "$private_file"; return 4; }
  rm -f "$private_file"
  export CHUDWARE_DEPLOY_DRAIN_FD=8 CHUDWARE_DEPLOY_DRAIN_OWNER_PID="$$"
  # Record ownership before begin: a transport failure may have acquired remotely.
  DRAIN_ACQUIRED=1
  deployment_drain_call prepare
}

deployment_drain_complete() {
  [[ "$DRAIN_ACQUIRED" = 1 ]] || return 0
  local attempts="${CHUDWARE_DEPLOY_HEALTH_ATTEMPTS:-30}" attempt
  [[ "$attempts" =~ ^[1-9][0-9]*$ && "$attempts" -le 300 ]] || return 1
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if deployment_drain_call health; then break; fi
    [[ "$attempt" -lt "$attempts" ]] || return 1
    sleep 1
  done
  if [[ "$DRAIN_BORROWED" = 1 ]]; then
    deployment_drain_call check && deployment_drain_call health
    return
  fi
  if ! deployment_drain_call complete; then
    # A lost release reply must never turn a failed rollout into open admission.
    deployment_drain_call begin || DRAIN_RELEASE_UNCONFIRMED=1
    return 1
  fi
  DRAIN_ACQUIRED=0
  unset CHUDWARE_DEPLOY_DRAIN_FD CHUDWARE_DEPLOY_DRAIN_OWNER_PID
  exec 8<&-
}

deployment_drain_abort() {
  [[ "$DRAIN_ACQUIRED" = 1 ]] || return 0
  if [[ "$MUTATED" != 0 ]]; then
    if [[ "$DRAIN_RELEASE_UNCONFIRMED" = 1 ]]; then
      echo "ERROR: deployment failed after mutation and maintenance reclosure is UNCONFIRMED. Treat the deployment as failed and admission as closed pending protected operator recovery; never force customer work or release a foreign lease." >&2
    else
      echo "ERROR: deployment failed after mutation; maintenance remains CLOSED. Do not stop cells, force jobs, or automatically roll back gateway/schema state. Recover the qualified payload and gateway using the protected maintenance lease, prove health and inactive cells, then explicitly release it." >&2
    fi
    return 1
  fi
  # A nested consumer never releases its caller's lease, even on local refusal.
  [[ "$DRAIN_BORROWED" = 0 ]] || return 0
  if ! deployment_drain_call release; then
    echo "ERROR: premutation refusal could not confirm release of this run's maintenance lease. Admission is UNKNOWN; use protected operator recovery and never release another operator's lease." >&2
    return 1
  fi
  DRAIN_ACQUIRED=0
  unset CHUDWARE_DEPLOY_DRAIN_FD CHUDWARE_DEPLOY_DRAIN_OWNER_PID
  exec 8<&-
}
