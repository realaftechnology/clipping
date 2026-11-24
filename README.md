# Real AF AI editors (clipping)

AI video editors (the "clipper agents") that turn raw podcast clips (Natalie's, plus moments the sourcing agent pulls from full episodes) into finished short-form posts, publish them, and track the results. One AI editor runs one **brand set**: one branded account on every platform (TikTok, Instagram, later YouTube, Facebook and X). Editor 01 = Share the Standard.

## Pieces

| Path | What it does |
|---|---|
| `flywheel.py` | The pipeline: `scan` → `prep` (crop + transcript) → `decide` (hook, song, caption, safety checks via headless Claude) → `render` + QA → review gate → `queue` → `post` → `track`/`stats`. `python3 flywheel.py run` does one unattended pass. |
| `sourcing.py` | The sourcing agent: takes each full episode from the exports folder, transcribes it, picks every substantive 1–3 min moment (headless Claude), cuts each at whole-sentence boundaries in the silences as a raw 16:9 clip, and holds it for approval (or, with `auto_approve`, hands it straight to the editors). `flywheel.py run` calls it once per pass. |
| `lib/render_clip.sh`, `lib/captions.py`, `lib/render.mjs` | Renders one clip: 1080x1920 crop, captions, hook box, music bed (ffmpeg + headless Chrome). |
| `bin/faces.swift` | Face detection for the crop (build: `swiftc -O bin/faces.swift -o bin/faces`). |
| `backends/zernio.py` | Posting and analytics through Zernio. `backends/dryrun.py` for tests. |
| `editors/<id>/editor.json`, `style.json` | One editor's brand set, accounts, schedule and look. |
| `hub/` | The central hub (hosted on Railway, no public address): the dashboard, chat with every editor, review, roles, audit log, the only copy of the Zernio key. Reached only through Cloudflare Access and the `gate/` Worker. |
| `worker.py` | Runs on every editing machine next to the loop: connects out to the hub, reports status, uploads review clips, applies approvals, directives and undos, answers chat, shares lessons. |
| `directives.py` | The checked actions a chat directive can take. Hard exclusions and safety checks are not among them. |
| `dashboard/server.py` | Local-only copy of the dashboard (http://127.0.0.1:8790); retired once the hub is live. `worker.py` reuses its pipeline view. |

## Setup on the editing Mac

1. `config.local.json` (not committed) with this machine's Google Drive paths: `source_root`, `music_dir`, and `episodes_folder_id` (the Drive folder of full episodes, for the sourcing agent).
2. `music/catalog.csv` (not committed): the song list.
3. `~/.config/ai-editors/secrets.json` (never committed, chmod 600): the hub credentials written by `hub/setup.sh worker`. `zernio_api_key` only until posting goes through the hub.
4. Tools: ffmpeg, whisper-cli, node + Chrome, the `claude` CLI signed in.
5. Run the loop: `while true; do python3 flywheel.py run; sleep 1800; done` and, next to it, `python3 worker.py` (the link to the hub).

## Central hub

Security model and decisions: Real AF project notes, `Context/Thoughts/2026-11-24 Central Hub - Security Architecture.md`.

- People sign in through Cloudflare Access (Google, allowlist) at the `gate/` Worker's workers.dev address; it passes
  through a Cloudflare Tunnel to the hub on Railway. The hub re-checks the signed identity on every request.
- Railway project: service `hub` (this repo, `railway.json`, volume at `/data`, a private bucket), service `tunnel`
  (image `cloudflare/cloudflared`, `tunnel --no-autoupdate run`, `TUNNEL_TOKEN`).
- Hub variables: `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD`, `HUB_OWNER_EMAIL`, `DATA_DIR=/data`, `TZ=America/New_York`, the bucket's
  variables, and secrets set by `hub/setup.sh hub` (`GATE_SECRET`, `ZERNIO_API_KEY`). Optional `ALERT_WEBHOOK` (e.g. an ntfy topic).
- A new machine: Matt creates a Cloudflare service token for it, adds it in the hub's Access panel (which shows its hub
  token once), runs `hub/setup.sh worker` there, then `python3 worker.py` next to the loop. Its editors appear in the
  Access panel and post only after Matt approves them. With `"zernio_via_hub": true` in `config.local.json` it posts
  through the hub and needs no Zernio key; `context_dir` points at its copy of the project notes, which the hub keeps in step.
