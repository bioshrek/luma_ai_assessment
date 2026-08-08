#!/usr/bin/env bash
# The reviewer's scenario, reproduced exactly: run, `kill -9` mid-flight, run again.
#
# The test suite crashes the pipeline from the inside, which proves every checkpoint is
# recoverable but never proves the *process* can be killed. This script does the one thing a
# test cannot: it has an external process send SIGKILL, which no handler can catch, no `finally`
# can soften, and no buffer survives.
#
#   bash scripts/demo_crash_resume.sh
#
# Uses a throwaway store under `.demo/`, so it never touches the working catalog. It ingests the
# real source from `config/sources.yaml` (so the kill lands in the middle of genuine work, not a
# fixture that finishes before it can be interrupted); override with RDP_DEMO_CONFIG for an
# offline run.

set -euo pipefail

cd "$(dirname "$0")/.."

DEMO_DIR="${RDP_DEMO_DIR:-.demo}"
STORE="$DEMO_DIR/store"
CATALOG="$STORE/catalog.sqlite"
KILL_AFTER="${RDP_DEMO_KILL_AFTER_EPISODES:-2}"
SOURCE="${RDP_DEMO_SOURCE:-pusht}"
CONFIG="${RDP_DEMO_CONFIG:-config}"

rule() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

committed() {
  [ -f "$CATALOG" ] || { echo 0; return; }
  sqlite3 "$CATALOG" "SELECT COUNT(*) FROM episodes WHERE status = 'COMMITTED';" 2>/dev/null || echo 0
}

catalog_dump() {
  [ -f "$CATALOG" ] || return 0
  sqlite3 -header -column "$CATALOG" \
    "SELECT episode_uid, status, qc_verdict, n_frames FROM episodes ORDER BY episode_uid;"
  echo
  sqlite3 -header -column "$CATALOG" \
    "SELECT run_id, status, COALESCE(resumed_from, '-') AS resumed_from FROM runs ORDER BY started_at;"
}

command -v sqlite3 >/dev/null || { echo "sqlite3 is required for this demo" >&2; exit 1; }

rule "0. Fresh store at $DEMO_DIR"
rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR"

rule "1. Start ingestion, then SIGKILL it once $KILL_AFTER episode(s) are committed"
# `set -m` puts the background job in its own process group, so the kill below reaches the
# python child and not just the `uv` wrapper. Killing only the wrapper would leave the real
# ingestion running, and the "resume" would then be two writers racing over one store.
set -m
uv run rdp run --source "$SOURCE" --store "$STORE" --config "$CONFIG" >"$DEMO_DIR/run1.log" 2>&1 &
PID=$!
set +m

killed=0
for _ in $(seq 1 600); do
  if ! kill -0 "$PID" 2>/dev/null; then break; fi
  if [ "$(committed)" -ge "$KILL_AFTER" ]; then
    # -9, not -TERM: the process gets no chance to tidy up. Whatever is on disk is all there is.
    kill -9 -"$PID" 2>/dev/null || kill -9 "$PID"
    killed=1
    echo "  SIGKILL sent to process group $PID after $(committed) committed episode(s)"
    break
  fi
  sleep 0.2
done
wait "$PID" 2>/dev/null || true

if [ "$killed" -ne 1 ]; then
  echo "  the run finished before it could be killed; lower RDP_DEMO_KILL_AFTER_EPISODES" >&2
  exit 1
fi

# A resume is only meaningful if nothing from run 1 is still alive.
if pgrep -g "$PID" >/dev/null 2>&1; then
  echo "  FAIL: part of run 1 survived the kill" >&2
  exit 1
fi

rule "2. State left behind by the kill"
catalog_dump
echo "  unfinished runs: $(sqlite3 "$CATALOG" "SELECT COUNT(*) FROM runs WHERE finished_at IS NULL;")"
echo "  held leases:     $(sqlite3 "$CATALOG" "SELECT COUNT(*) FROM episode_state WHERE lease_owner IS NOT NULL;")"
echo "  orphan *.tmp:    $(find "$STORE" -name '*.tmp' | wc -l | tr -d ' ')"

rule "3. Restart — expect a resume, not a restart"
uv run rdp run --source "$SOURCE" --store "$STORE" --config "$CONFIG" 2>&1 | tee "$DEMO_DIR/run2.log"

rule "4. State after the resume"
catalog_dump
echo "  orphan *.tmp:    $(find "$STORE" -name '*.tmp' | wc -l | tr -d ' ')"

rule "5. Run a third time — expect no new data, and no work"
uv run rdp run --source "$SOURCE" --store "$STORE" --config "$CONFIG" 2>&1 | tee "$DEMO_DIR/run3.log"

rule "Verdict"
resumed_from=$(sqlite3 "$CATALOG" "SELECT COALESCE(resumed_from, '') FROM runs ORDER BY started_at LIMIT 1 OFFSET 1;")
run1_status=$(sqlite3 "$CATALOG" "SELECT status FROM runs ORDER BY started_at LIMIT 1;")
skipped=$(grep -c 'skipped_already_processed' "$DEMO_DIR/run3.log" || true)
[ "$run1_status" = "INTERRUPTED" ] || { echo "FAIL: run 1 is $run1_status, expected INTERRUPTED"; exit 1; }
[ -n "$resumed_from" ] || { echo "FAIL: run 2 did not record a resume"; exit 1; }
[ "$skipped" -ge 1 ] || { echo "FAIL: run 3 printed no skip counter"; exit 1; }
echo "  run 1 was left INTERRUPTED; run 2 resumed from $resumed_from"
grep -E 'skipped_already_processed|committed|fetched|normalized|failed' "$DEMO_DIR/run3.log"
echo "  OK: crash resumed, re-run was a no-op."
