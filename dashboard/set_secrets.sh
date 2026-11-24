#!/bin/zsh
# One-time: sets the hosted dashboard's secrets on Railway and saves the Mac's sync token.
# Run from the AI Editors folder after `railway link`. Nothing secret is printed.
set -e
cd "${0:A:h}/.."
S=~/.config/ai-editors/secrets.json
printf "Choose the dashboard password (you'll give this to Natalie): "
read -rs PW; echo
[[ ${#PW} -ge 10 ]] || { echo "Use at least 10 characters. Nothing was changed."; exit 1; }
TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
KEY=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['zernio_api_key'])" "$S")
railway variables --set "DASHBOARD_PASSWORD=$PW" --set "INGEST_TOKEN=$TOKEN" --set "ZERNIO_API_KEY=$KEY" \
  --set "DATA_DIR=/data" --set "TZ=America/New_York" >/dev/null
python3 - "$S" "$TOKEN" <<'PY'
import json, os, sys
p, tok = sys.argv[1:]
s = json.load(open(p)); s['dashboard_ingest_token'] = tok
json.dump(s, open(p, 'w'), indent=2); os.chmod(p, 0o600)
PY
unset PW TOKEN KEY
echo "Done: password, sync token and Zernio key are set on Railway; the Mac's sync token is saved."
