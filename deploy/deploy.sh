#!/usr/bin/env bash
# Pull-based continuous deployment for cs2-tracker.
#
# Triggered every few minutes by cs2-tracker-deploy.timer (or run by hand).
# Two things can trigger a deploy:
#   1. origin/main has moved ahead of us  -> fast-forward to it
#   2. the running service (recorded in .deployed_commit) is not the
#      commit we have now - covers commits made directly on the Pi
# Incoming code is byte-compiled and import-checked before the service
# restarts, then health-checked (HTTP 200 AND healthy:true from the
# self-healer); on failure the previously running commit is redeployed
# automatically.
#
# Safety: local commits not yet pushed are never lost (we only fast-forward
# to origin), and a dirty working tree blocks deploys instead of being
# overwritten. Only origin/main is ever deployed - PR branches never run.
set -u

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
SERVICE=cs2-tracker
PORT=8092
PYTHON="$REPO_DIR/venv/bin/python"   # project venv
LOG="$REPO_DIR/deploy.log"
MARKER="$REPO_DIR/.deployed_commit"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

cd "$REPO_DIR" || exit 1

# --- sync with origin (best effort; the Pi may also be offline) ---
if git remote get-url origin >/dev/null 2>&1; then
    if git fetch origin main --quiet 2>>"$LOG"; then
        HEAD_C=$(git rev-parse HEAD)
        REMOTE_C=$(git rev-parse origin/main)
        if [ "$HEAD_C" != "$REMOTE_C" ]; then
            if git merge-base --is-ancestor "$HEAD_C" "$REMOTE_C"; then
                # strictly behind: safe to fast-forward, unless work is
                # sitting uncommitted in the tree
                if git diff --quiet && git diff --cached --quiet; then
                    log "updating to origin/main ${REMOTE_C:0:12} (was ${HEAD_C:0:12})"
                    git reset --hard "$REMOTE_C" >>"$LOG" 2>&1
                else
                    log "skip update: dirty working tree - commit or stash first"
                fi
            elif git merge-base --is-ancestor "$REMOTE_C" "$HEAD_C"; then
                log "note: local is ahead of origin - push when ready"
            else
                log "skip update: local and origin/main diverged - needs a human"
            fi
        fi
    else
        log "note: fetch failed (offline?)"
    fi
fi

# --- deploy whatever HEAD is now, if the service isn't already on it ---
HEAD_C=$(git rev-parse HEAD)
RUNNING_C=$(cat "$MARKER" 2>/dev/null || echo "")
if [ "$HEAD_C" = "$RUNNING_C" ]; then
    exit 0  # running code matches, nothing to do
fi
FAILED_C=$(cat "$REPO_DIR/.deploy_failed" 2>/dev/null || echo "")
if [ "$HEAD_C" = "$FAILED_C" ]; then
    exit 0  # already tried this exact commit and it failed health checks
fi
log "deploying ${HEAD_C:0:12} (service was on ${RUNNING_C:-nothing yet})"

health_ok() {
    # HTTP 200 AND self-healer reports healthy:true
    curl -sf -m 5 "http://127.0.0.1:$PORT/healthz" | grep -q '"healthy":true'
}

restart_service() {
    sudo systemctl restart "$SERVICE"
    for _ in $(seq 1 12); do
        sleep 2.5
        health_ok && return 0
    done
    return 1
}

# Gate: incoming code must byte-compile and import cleanly before we restart.
if ! "$PYTHON" -m compileall -q app.py healer.py store.py faceit.py steam.py gsi.py || \
   ! "$PYTHON" -c "import app" >>"$LOG" 2>&1; then
    log "CHECKS FAILED on $HEAD_C - not deploying"
    exit 1
fi

if restart_service; then
    echo "$HEAD_C" > "$MARKER"
    rm -f "$REPO_DIR/.deploy_failed"
    log "deployed ${HEAD_C:0:12} and healthy"
    exit 0
fi

log "HEALTH CHECK FAILED after deploy"
echo "$HEAD_C" > "$REPO_DIR/.deploy_failed"
if [ -n "$RUNNING_C" ]; then
    log "rolling back to ${RUNNING_C:0:12}"
    git reset --hard "$RUNNING_C" >>"$LOG" 2>&1
    sudo systemctl restart "$SERVICE"
    if restart_service; then
        log "rollback to ${RUNNING_C:0:12} successful"
    else
        log "ROLLBACK ALSO UNHEALTHY - service state:"
        systemctl status "$SERVICE" --no-pager -l | tail -20 | tee -a "$LOG"
    fi
else
    log "no previously deployed commit to roll back to - service state:"
    systemctl status "$SERVICE" --no-pager -l | tail -20 | tee -a "$LOG"
fi
exit 1
