#!/usr/bin/env bash
# Convenience wrapper: load .env, dispatch to one of the python scripts.
#
#   ./run.sh pipeline    /path/to/one.pdf
#   ./run.sh batch       /path/to/folder
#   ./run.sh synth       /path/to/one.pdf
#   ./run.sh synth-batch /path/to/folder
#   ./run.sh sync        --set ./set/ --agent-url https://...
#   ./run.sh seo         --card library_card.json --html
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# 1) Load .env if present (export every KEY=VALUE)
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# 2) Sanity check the key
if [[ -z "${UPSTAGE_API_KEY:-}" || "$UPSTAGE_API_KEY" == "up_REPLACE_ME" ]]; then
  echo "❌ UPSTAGE_API_KEY is not set."
  echo "   Edit $HERE/.env and replace 'up_REPLACE_ME' with your key from"
  echo "   https://console.upstage.ai/api-keys"
  exit 2
fi

# 3) Dispatch
sub="${1:-help}"; shift || true
case "$sub" in
  pipeline)    exec python3 pipeline.py    "$@" ;;
  batch)       exec python3 batch.py       "$@" ;;
  synth)       exec python3 synth.py       "$@" ;;
  synth-batch) exec python3 synth_batch.py "$@" ;;
  sync)        exec python3 notion_sync.py "$@" ;;
  seo)         exec python3 seo_meta.py    "$@" ;;
  help|*)
    cat <<HELP
Usage: ./run.sh <subcommand> [args]

  pipeline    one PDF → Parse + Classify + Extract → JSON
  batch       whole folder, parallel
  synth       one PDF → synthetic HTML (Phase 2)
  synth-batch whole folder synthesis + leakage verification
  sync        upsert use-case set to Notion (needs NOTION_TOKEN)
  seo         emit SEO/OG/JSON-LD for a Library card

Configure: edit ./.env  (UPSTAGE_API_KEY required)
HELP
    ;;
esac
