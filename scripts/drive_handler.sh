#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="$(hostname -s)"
LOCAL_LOG="/var/log/borg-sync/${HOST}.log"

mkdir -p "$(dirname "$LOCAL_LOG")" 2>/dev/null || true

log_local() { echo "[$(date -Iseconds)] $*" | tee -a "$LOCAL_LOG"; }

DRIVE_LABEL="$(python3 -c "import yaml; print(yaml.safe_load(open('${REPO_DIR}/manifest/manifest.yaml'))['drive']['label'])")"
DRIVE_UUID="$(python3 -c "import yaml; print(yaml.safe_load(open('${REPO_DIR}/manifest/manifest.yaml'))['drive'].get('expected_uuid',''))")"
DRIVE_TIMEOUT="$(python3 -c "import yaml; print(yaml.safe_load(open('${REPO_DIR}/manifest/manifest.yaml'))['drive'].get('mount_timeout',30))")"

CHECK_ARGS=(--label "$DRIVE_LABEL" --timeout "$DRIVE_TIMEOUT")
[[ -n "$DRIVE_UUID" ]] && CHECK_ARGS+=(--uuid "$DRIVE_UUID")

if ! MOUNT_POINT="$("${REPO_DIR}/scripts/check_drive_mounted.sh" "${CHECK_ARGS[@]}")"; then
    log_local "ERROR: drive '${DRIVE_LABEL}' not verified mounted, aborting"
    exit 1
fi

DRIVE_LOG="${MOUNT_POINT}/logs/${HOST}.log"
mkdir -p "$(dirname "$DRIVE_LOG")" 2>/dev/null || true

log() {
    local msg="[$(date -Iseconds)] $*"
    echo "$msg" | tee -a "$LOCAL_LOG" >>"$DRIVE_LOG" 2>/dev/null || echo "$msg" | tee -a "$LOCAL_LOG"
}

log "verified $DRIVE_LABEL mounted at $MOUNT_POINT"

log "=== drive attached, starting sync sequence for $HOST ==="

log "[1/4] refreshing installed-software inventory (read-only)"
if ! ansible-playbook -i "${REPO_DIR}/ansible/inventory/hosts.yml" \
        "${REPO_DIR}/ansible/playbooks/gather_facts.yml" \
        --extra-vars "target_host=${HOST}" >>"$LOCAL_LOG" 2>&1; then
    log "WARNING: fact gathering failed, continuing anyway"
fi

log "[2/4] ledger-aware live sync"
if ! python3 "${REPO_DIR}/scripts/ledger_sync.py" \
        --host "$HOST" \
        --drive-mount "$MOUNT_POINT" \
        --manifest "${REPO_DIR}/manifest/manifest.yaml" >>"$LOCAL_LOG" 2>&1; then
    log "WARNING: livesync reported conflicts — check ${LOCAL_LOG} for which folders were skipped"
fi

log "[3/4] regenerating borgmatic config from manifest"
python3 "${REPO_DIR}/scripts/render_borgmatic_config.py" \
    --host "$HOST" \
    --manifest "${REPO_DIR}/manifest/manifest.yaml" \
    --drive-mount "$MOUNT_POINT" \
    --output "${REPO_DIR}/borgmatic/generated/${HOST}.yaml" >>"$LOCAL_LOG" 2>&1

log "[4/4] running borgmatic backup"
if borgmatic -c "${REPO_DIR}/borgmatic/generated/${HOST}.yaml" create --stats >>"$LOCAL_LOG" 2>&1; then
    log "backup succeeded"
else
    log "ERROR: borgmatic backup failed — see ${LOCAL_LOG}"
    exit 1
fi

CHECK_WEEKDAY=$(python3 -c "import yaml,sys; print(yaml.safe_load(open('${REPO_DIR}/manifest/manifest.yaml'))['borg']['check_weekday'])")
if [ "$(date +%u)" -eq "$CHECK_WEEKDAY" ]; then
    log "scheduled weekly integrity check"
    if borgmatic -c "${REPO_DIR}/borgmatic/generated/${HOST}.yaml" check >>"$LOCAL_LOG" 2>&1; then
        log "weekly check passed"
    else
        log "ERROR: weekly check FAILED — investigate before trusting this repo"
    fi
fi

log "=== sync sequence complete ==="
