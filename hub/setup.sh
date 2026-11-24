#!/bin/zsh
# Secrets for the central hub. Matt runs this himself; nothing secret is printed or put on a command line.
#   hub/setup.sh hub      after the Railway project and the Cloudflare tunnel exist (run in the AI Editors folder,
#                         linked to the hub project with `railway link`): sets the gate secret on Railway and on the
#                         Cloudflare Worker, moves the Zernio key to the hub, and takes the tunnel token you paste.
#   hub/setup.sh worker   on each editing machine, after Matt adds it in the hub's Access panel: saves its
#                         Cloudflare service token and hub token to ~/.config/ai-editors/secrets.json (chmod 600).
set -e
cd "${0:A:h}/.."
S=~/.config/ai-editors/secrets.json
mkdir -p ${S:h}; [[ -f $S ]] || echo '{}' > $S; chmod 600 $S

secret() { printf "%s (hidden): " "$1"; read -rs REPLY; echo; [[ -n $REPLY ]] || { echo "Nothing entered. Nothing was changed."; exit 1; }; }
save() {  # save key=value pairs into the secrets file, values on stdin (one per line, in order)
  python3 -c '
import json, os, sys
p, keys = sys.argv[1], sys.argv[2:]
vals = sys.stdin.read().split("\n")
s = json.load(open(p))
for k, v in zip(keys, vals):
    s[k] = v
json.dump(s, open(p, "w"), indent=2); os.chmod(p, 0o600)' "$S" "$@"
}

case $1 in
hub)
  GATE=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  print -rn -- "$GATE" | railway variables --service hub --set-from-stdin GATE_SECRET --skip-deploys >/dev/null
  if ! railway variables --service hub --kv 2>/dev/null | grep -q '^AUDIT_KEY='; then  # set once: changing it breaks the audit chain
    python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" | railway variables --service hub --set-from-stdin AUDIT_KEY --skip-deploys >/dev/null
  fi
  python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['zernio_api_key'], end='')" "$S" \
    | railway variables --service hub --set-from-stdin ZERNIO_API_KEY --skip-deploys >/dev/null
  secret "Paste the Cloudflare tunnel token"
  print -rn -- "$REPLY" | railway variables --service tunnel --set-from-stdin TUNNEL_TOKEN >/dev/null
  (cd gate && print -rn -- "$GATE" | npx wrangler@4 secret put GATE_SECRET >/dev/null)
  unset GATE REPLY
  echo "Done: gate secret set on the hub and the Worker, Zernio key moved to the hub, tunnel token set."
  ;;
worker)
  printf "Hub address (e.g. https://realaf-hub.<you>.workers.dev): "; read -r URL
  printf "This machine's Cloudflare service token Client ID: "; read -r CID
  secret "Its Client Secret"; CSEC=$REPLY
  secret "Its hub token (from the hub's Access panel)"; HTOK=$REPLY
  printf '%s\n%s\n%s\n%s' "$URL" "$CID" "$CSEC" "$HTOK" | save hub_url cf_access_client_id cf_access_client_secret hub_worker_token
  unset CSEC HTOK REPLY
  echo "Saved to $S. Test it: python3 worker.py --once"
  ;;
*)
  echo "usage: hub/setup.sh hub | worker"; exit 1 ;;
esac
