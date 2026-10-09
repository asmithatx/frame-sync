# Mongo data model for frame-sync

The interview question will be "why did you model it this way?" — so the
reasoning is written down here, next to the code.

## Access patterns (decide the model from these, not from the ERD)

| Who      | Operation                                   | Frequency        |
|----------|---------------------------------------------|------------------|
| Frame    | read **its whole manifest** on change/boot  | rare, must be cheap |
| Frame    | heartbeat: update one device doc            | every 60 s / frame |
| Phone    | append 1–20 photos to a playlist            | bursty           |
| Phone    | assign playlist ↔ frame, pick active        | rare             |
| Phone    | list playlists with counts / thumbnails     | on app open      |

Reads of the full manifest dominate design. Writes are appends.

## Collections

### `media` — one document per uploaded photo (referenced, not embedded)
```json
{
  "_id": ObjectId,
  "owner_id": "andrew",
  "sha256": "…",
  "filename": "2026-10-03_1234.jpg",
  "mime": "image/jpeg",
  "storage_key": "originals/andrew/2026/10/<sha>.jpg",
  "variants": { "1920": "derived/…/<sha>_1920.jpg" },
  "width": 4032, "height": 3024, "bytes": 2318455,
  "taken_at": ISODate, "uploaded_at": ISODate,
  "tags": ["kallie", "lake"],
  "caption": "…"
}
```
Why its own collection: a photo can be in many playlists, it has
metadata the frame never needs, and it's the thing Atlas Search / Vector
Search indexes later. Embedding the full media doc inside every playlist
would duplicate bytes and make "delete this photo everywhere" a scan.

### `playlists` — items **embedded** as an array of thin references
```json
{
  "_id": ObjectId,
  "owner_id": "andrew",
  "slug": "family",          // becomes the folder name on the Pi
  "name": "Family",
  "items": [
    { "media_id": ObjectId, "added_at": ISODate, "order": 10 },
    …
  ],
  "updated_at": ISODate
}
```
Why embed: a playlist *is* its ordered list; the whole thing is read and
rewritten as a unit; reordering is one update; the array stays well
under the 16 MB doc limit (even 10k items × ~40 B ≈ 400 KB). The
classic "unbounded array" smell doesn't apply because a playlist has a
natural human-scale bound. `$push` with `$each`/`$position` handles
appends; `$pull` handles removal.

Trade-off to say out loud: appending to a large array rewrites the
document on disk (WiredTiger is copy-on-write per doc), and change
streams emit the array diff via `updateDescription`. Fine at this scale;
at 100k-item playlists you'd bucket (one doc per N items) instead.

### `devices` — one per frame; embeds assignment + latest status
```json
{
  "_id": "frame-kitchen",         // device_id = natural key
  "owner_id": "andrew",
  "token_hash": "…",              // sha256 of the device bearer token
  "playlist_ids": [ObjectId, …],
  "active_playlist": "family",
  "manifest_version": 42,         // bumped by any change affecting this frame
  "status": {
    "seen_at": ISODate, "files": 143, "bytes": 71234567, "last_error": null
  },
  "width": 1920, "height": 1080    // for variant selection
}
```
Why `manifest_version` lives here: the frame subscribes to **its own
document only** (change stream with a `$match` on `documentKey._id`).
Anything that should wake the frame — playlist edit, new photo,
reassignment — bumps this one field, so the frame never has to watch
`playlists` or `media` directly. That keeps the frame's change-stream
filter trivial and its privilege narrow.

Why status is embedded rather than a `heartbeats` collection: we only
care about the latest; one `$set` per minute per device is cheap; no
unbounded growth. If you wanted history you'd use a time-series
collection with TTL, not an array.

## The manifest query (aggregation)
See `manifest.py`. `devices` ─$lookup→ `playlists` ─$unwind items─$lookup→ `media`
─$project→ flat list. Indexes that make it cheap:

```
media:      { _id: 1 }            // default; the $lookup target
playlists:  { owner_id: 1, slug: 1 } unique
devices:    { owner_id: 1 }
media:      { owner_id: 1, taken_at: -1 }   // phone's "recent" list
media:      { sha256: 1 } unique            // dedupe uploads
```

## Keeping `manifest_version` honest
A write to `playlists` or `media` must bump every affected device. Two
options, and the choice is itself a good interview answer:

1. **App-layer, in a transaction** — the FastAPI handler does the
   playlist `$push` and the `devices.updateMany({playlist_ids: pid}, {$inc: {manifest_version: 1}})`
   inside a multi-document transaction. Clear, testable, needs a replica
   set (Atlas always is).
2. **Trigger-ish** — a small worker watching `playlists`/`media` change
   streams fans out the bump. Decoupled, but introduces a second process
   and an eventual-consistency window.

The scaffold uses option 1 (see `seed.py`'s `add_to_playlist` helper).

## What I'd change at 10,000 frames
- Shard `devices` on `{ owner_id: 1, _id: 1 }` (hashed `_id` if owners
  are very uneven); `playlists` and `media` on `owner_id`.
- Move `status` writes to a time-series collection to keep the hot
  `devices` docs small and avoid 10k docs rewritten a minute.
- Change streams: one per frame is 10k open cursors — fine for Atlas
  M30+, but a fan-out service (one stream → pub/sub → frames over MQTT)
  is the real answer.
