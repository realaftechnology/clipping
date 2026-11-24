# Real AF AI editors (clipping)

AI video editors that turn the raw podcast clips Natalie selects into finished short-form posts, publish them, and track the results. One AI editor runs one **brand set**: one branded account on every platform (TikTok, Instagram, later YouTube, Facebook and X). Editor 01 = Share the Standard.

## Pieces

| Path | What it does |
|---|---|
| `flywheel.py` | The pipeline: `scan` → `prep` (crop + transcript) → `decide` (hook, song, caption, safety checks via headless Claude) → `render` + QA → review gate → `queue` → `post` → `track`/`stats`. `python3 flywheel.py run` does one unattended pass. |
| `lib/render_clip.sh`, `lib/captions.py`, `lib/render.mjs` | Renders one clip: 1080x1920 crop, captions, hook box, music bed (ffmpeg + headless Chrome). |
| `bin/faces.swift` | Face detection for the crop (build: `swiftc -O bin/faces.swift -o bin/faces`). |
| `backends/zernio.py` | Posting and analytics through Zernio. `backends/dryrun.py` for tests. |
| `editors/<id>/editor.json`, `style.json` | One editor's brand set, accounts, schedule and look. |
| `dashboard/server.py` | Operations dashboard on the editing Mac (http://127.0.0.1:8790). With a hosted copy configured, it pushes data there every minute and applies approvals made there. |
| `dashboard/cloud.py` | Hosted copy of the dashboard (Railway) behind a password, for Natalie and Matt. |

## Setup on the editing Mac

1. `config.local.json` (not committed) with this machine's Google Drive paths: `source_root`, `music_dir`.
2. `music/catalog.csv` (not committed): the song list.
3. `~/.config/ai-editors/secrets.json` (never committed): `zernio_api_key`, and for the hosted dashboard `dashboard_url` and `dashboard_ingest_token`.
4. Tools: ffmpeg, whisper-cli, node + Chrome, the `claude` CLI signed in.
5. Run the loop: `while true; do python3 flywheel.py run; sleep 1800; done` and the dashboard: `python3 dashboard/server.py`.

## Hosted dashboard

Deploy `dashboard/` to Railway with a volume mounted at `/data` and these variables: `DATA_DIR=/data`, `DASHBOARD_PASSWORD`, `INGEST_TOKEN` (same value as `dashboard_ingest_token` on the Mac).

The output recipe, safety rules and decisions live in the Real AF project notes (`Context/AI Editors - Output Spec.md`), not in this repo.
