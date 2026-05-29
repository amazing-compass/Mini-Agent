#!/usr/bin/env bash
# Fresh re-run of the CURRENT fork (Mini-Agent, post04+) on the SAME 50
# verified instances / model / prompt / ceilings as the stock baseline
# (Mini-Agent-main/run_baseline.sh), so the fork-vs-stock comparison is
# same-period and apples-to-apples. Output is resumable.
#
# Differs from the baseline only in the agent internals being tested:
# HA router + planning (TodoWrite) + cache-aware compaction + permissions.
#
# Usage:
#   ./run_fork.sh              # full 50-task run (~3h, real API $)
#   SMOKE=1 ./run_fork.sh      # single-instance smoke (cheap end-to-end check)
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUNBUFFERED=1   # flush progress live so `tail -f` shows it immediately

OUT="benchmark_runs/fork_rerun"
IDS="$(cat benchmark_runs/fork_rerun_instance_ids.txt)"

if [[ "${SMOKE:-0}" == "1" ]]; then
  OUT="benchmark_runs/fork_rerun_smoke"
  IDS="astropy__astropy-14539"   # same tiny task the baseline smoke used
  echo "🔬 SMOKE MODE → 1 instance → $OUT"
fi

uv run python -m mini_agent.benchmarks.swebench.cli run \
  --subset verified \
  --instance-ids "$IDS" \
  --output "$OUT" \
  --run-id "$(basename "$OUT")" \
  --model-name mini-agent-fork-rerun-deepseek-v4-pro \
  --max-steps 50 \
  --timeout 600 \
  --concurrency 1
