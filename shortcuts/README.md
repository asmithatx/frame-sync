# iPhone → frames: the Shortcut

One Share-sheet action: pick photos in Photos, Share → **Send to Frames**, pick
a playlist, done. No app to build; Shortcuts does HEIC→JPEG and resizing on
the phone so the Edge Function stays tiny and the free-tier bucket stays small.

## Build it (Shortcuts app → + → name "Send to Frames")

Settings (ⓘ): **Show in Share Sheet** ✓ · Share Sheet Types: **Images** only.

| # | Action | Config |
|---|--------|--------|
| 1 | Receive **Images** from Share Sheet | If no input: *Select Photos* |
| 2 | Choose from List | List: `family`, `ai-art`, `inbox` → variable **Playlist** |
| 3 | Repeat with Each (Shortcut Input) | |
| 4 | ↳ Get Details of Images | **Date Taken** → variable **Taken** |
| 5 | ↳ Resize Image | Longest edge **2048** px (fine for 1080p and 4K frames) |
| 6 | ↳ Convert Image | **JPEG**, quality 0.85, *Preserve Metadata* off |
| 7 | ↳ Get Contents of URL | see below |
| 8 | ↳ Get Dictionary Value `deduped` from Contents of URL | optional, for the notification |
| 9 | End Repeat | |
| 10 | Show Notification | "Sent {Repeat Results count} photo(s) to {Playlist}" |

### Step 7 — Get Contents of URL
- URL: `https://<project-ref>.supabase.co/functions/v1/ingest`
- Method: **POST**
- Headers: `Authorization` = `Bearer <your upload token>`
- Request Body: **Form**
  - `file` = **File** → *Converted Image* (from step 6)
  - `playlist` = **Text** → *Playlist*
  - `taken_at` = **Text** → *Taken* formatted ISO 8601 (Format Date → Custom `yyyy-MM-dd'T'HH:mm:ssZZZZZ`)

Local testing (`supabase start`): point the URL at your Mac's LAN IP /
Tailscale IP on port 54321 — `http://100.x.y.z:54321/functions/v1/ingest`.
That's the Tailscale track showing up early.

## Upload token
Generate one, store only its hash:
```sql
-- run as the owner in the SQL editor
insert into public.upload_tokens (owner_id, token_hash, label)
values (auth.uid(), encode(digest('paste-a-long-random-string', 'sha256'), 'hex'), 'iPhone');
```
Put the plaintext in the Shortcut's Authorization header. Revoke = delete the row.

## Why not Supabase Auth on the phone?
Shortcuts can't run an OAuth/magic-link flow and has no secure place to keep a
refresh token. A scoped, revocable bearer token checked by an Edge Function is
the honest answer for a Shortcut client. The PWA (later) uses real Auth.

## Verify end-to-end
1. Shortcut returns `{"media_id": …, "deduped": false}`.
2. `select name, manifest_version from devices;` — every frame assigned to that
   playlist went up by one.
3. Agent log on the Pi: `broadcast: manifest v<N>` then `+ family/<file>.jpg`.
4. Send the same photo again → `deduped: true`, versions unchanged.
