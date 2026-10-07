#!/usr/bin/env bash
# Build the place with Rojo and publish it via Roblox Open Cloud.
# Needs: ROBLOX_API_KEY (scope universe-places:write), UNIVERSE_ID, PLACE_ID.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${ROBLOX_API_KEY:?set ROBLOX_API_KEY}"
: "${UNIVERSE_ID:?set UNIVERSE_ID}"
: "${PLACE_ID:?set PLACE_ID}"
VERSION_TYPE="${VERSION_TYPE:-Published}" # or Saved

mkdir -p build
rojo build default.project.json -o build/NeonTowerObby.rbxlx

curl --fail-with-body -sS -X POST \
  "https://apis.roblox.com/universes/v1/${UNIVERSE_ID}/places/${PLACE_ID}/versions?versionType=${VERSION_TYPE}" \
  -H "x-api-key: ${ROBLOX_API_KEY}" \
  -H "Content-Type: application/xml" \
  --data-binary @build/NeonTowerObby.rbxlx
echo
echo "Published. Live at https://www.roblox.com/games/${PLACE_ID}"
