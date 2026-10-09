# frame-sync

Cloud sync for Dynaframe picture frames (Raspberry Pi). One agent, three
swappable backends — each one a deliberate exercise for a specific stack:

| Track | Backend dir           | Push channel            | Status |
|-------|-----------------------|-------------------------|--------|
| 1     | `backends/mongo/`     | Change Streams          | scaffolded |
| 2     | `backends/supabase/`  | Realtime Broadcast from DB | scaffolded + SQL tested |
| 3     | `backends/immich/`    | poll over Tailscale     | todo |

```
agent/        backend-agnostic: diff manifest vs disk, atomic downloads, Dynaframe poke
backends/     one package per backend implementing agent/backend.py:SyncBackend
docs/         design notes + interview prep
```

## Run the Mongo track locally (no Pi needed)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill MONGO_URI (Atlas M0 is free) + STORAGE_BASE_URL

# 1. create collections/indexes/sample playlists + two devices
python -m backends.mongo.seed

# 2. run an agent as "frame-kitchen" writing into ./frames
DEVICE_ID=frame-kitchen DEVICE_TOKEN=dev-token-frame-kitchen MEDIA_ROOT=./frames STATE_DIR=./state \
  python -m agent.main

# 3. in another shell, simulate a phone upload -> watch the agent react
python -m backends.mongo.seed --add family 2026-10-03_lake.jpg
```

The sample media rows point at keys that don't exist in your bucket yet,
so downloads will 404 until you upload something — the agent logs the
error, leaves `manifest_version` unbumped, and retries on the next pass.
Drop a real JPEG into the bucket at the printed `storage_key` to see it land.

## Atlas setup (5 minutes)
1. Free M0 cluster → Database Access → add user `frame` with **custom role**:
   `read` on `framesync.media`, `framesync.playlists`; `readWrite` on `framesync.devices`.
   (Least privilege is a talking point: the frame can't touch your photo metadata.)
2. Network Access → your home IP for now; the Tailscale track replaces this.
3. Copy the SRV connection string into `.env`.

## On the Pi
`systemd/frame-sync.service` (todo) — `After=network-online.target`, `Restart=always`,
env from `/etc/frame-sync.env`.

## Run the Supabase track locally

```bash
supabase start                      # local stack (Docker); prints URL + keys
supabase db reset                   # applies supabase/migrations + seed.sql
supabase functions serve ingest --no-verify-jwt

# agent as frame-kitchen
BACKEND=supabase SUPABASE_URL=http://127.0.0.1:54321 SUPABASE_ANON_KEY=<printed anon key> \
DEVICE_EMAIL=frame-kitchen@frames.local DEVICE_PASSWORD=frame-kitchen-pass \
MEDIA_ROOT=./frames STATE_DIR=./state python -m agent.main

# simulate the iPhone Shortcut
curl -F file=@some.jpg -F playlist=family -H "Authorization: Bearer dev-upload-token" \
     http://127.0.0.1:54321/functions/v1/ingest
```

No Docker? `tests/run_supabase_sql.sh` loads the migration + seed onto any plain
Postgres (with a small shim for the `auth`/`realtime`/`storage` schemas) and runs
22 RLS/trigger assertions as the frame, owner and service roles.

Layout: `supabase/migrations/0001_frame_sync.sql` (schema, RLS, triggers, Broadcast,
RPCs, storage policies), `supabase/functions/ingest/` (Deno Edge Function),
`shortcuts/README.md` (the iOS Shortcut), `backends/supabase/backend.py`.

## Docs
- `backends/mongo/schema.md` — Mongo data model and the reasoning behind it
- `supabase/migrations/0001_frame_sync.sql` — header comment is the Supabase design note

## Why three backends
This is a hobby project that doubles as a way to learn three stacks properly:
the same agent interface, three honest implementations, and a written note on
what each one would need at 10k frames. See the per-backend docs.
