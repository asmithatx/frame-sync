// POST /functions/v1/ingest
//   Authorization: Bearer <upload token>        (long-lived, hashed in public.upload_tokens)
//   multipart/form-data: file=<jpeg>, playlist=<slug>, [taken_at=<iso>], [caption=<text>]
//
// This is the only code that runs with the service role. Everything it does on
// behalf of the user is scoped by owner_id it looked up from the token, and the
// DB triggers take care of bumping/broadcasting to frames.
//
// Deploy:  supabase functions deploy ingest --no-verify-jwt
// Local:   supabase functions serve ingest --no-verify-jwt --env-file supabase/.env.local

import { createClient } from "npm:@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const MAX_BYTES = 25 * 1024 * 1024;

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

async function sha256Hex(buf: ArrayBuffer): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256Text(s: string): Promise<string> {
  return sha256Hex(new TextEncoder().encode(s));
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json(405, { error: "POST only" });

  // --- 1. who is uploading? ------------------------------------------------
  const auth = req.headers.get("authorization") ?? "";
  const token = auth.replace(/^Bearer\s+/i, "").trim();
  if (!token) return json(401, { error: "missing upload token" });

  const admin = createClient(SUPABASE_URL, SERVICE_KEY, { auth: { persistSession: false } });
  const { data: tok, error: tokErr } = await admin
    .from("upload_tokens")
    .select("id, owner_id")
    .eq("token_hash", await sha256Text(token))
    .maybeSingle();
  if (tokErr) return json(500, { error: tokErr.message });
  if (!tok) return json(401, { error: "invalid upload token" });

  // --- 2. parse the upload --------------------------------------------------
  let form: FormData;
  try {
    form = await req.formData();
  } catch {
    return json(400, { error: "expected multipart/form-data" });
  }
  const file = form.get("file");
  const playlist = String(form.get("playlist") ?? "inbox").toLowerCase();
  if (!(file instanceof File)) return json(400, { error: "file field required" });
  if (!/^[a-z0-9-]{1,40}$/.test(playlist)) return json(400, { error: "bad playlist slug" });
  if (file.size > MAX_BYTES) return json(413, { error: `file > ${MAX_BYTES} bytes` });

  const mime = file.type || "image/jpeg";
  const ext = { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "video/mp4": "mp4" }[mime];
  if (!ext) return json(415, { error: `unsupported type ${mime}; convert HEIC→JPEG in the Shortcut` });

  const bytes = await file.arrayBuffer();
  const sha = await sha256Hex(bytes);
  const storagePath = `${tok.owner_id}/${sha}.${ext}`;

  // --- 3. dedupe: same owner + same bytes = same media row -------------------
  const { data: existing } = await admin
    .from("media")
    .select("id")
    .eq("owner_id", tok.owner_id)
    .eq("sha256", sha)
    .maybeSingle();

  let mediaId = existing?.id as string | undefined;

  if (!mediaId) {
    // --- 4. object first, then row: a row without an object is worse than the reverse
    const { error: upErr } = await admin.storage
      .from("media")
      .upload(storagePath, bytes, { contentType: mime, upsert: false });
    if (upErr && !/already exists|duplicate/i.test(upErr.message)) {
      return json(500, { error: `storage: ${upErr.message}` });
    }

    const { data: row, error: insErr } = await admin
      .from("media")
      .insert({
        owner_id: tok.owner_id,
        sha256: sha,
        filename: `${(form.get("taken_at") ?? new Date().toISOString()).toString().slice(0, 10)}_${sha.slice(0, 8)}.${ext}`,
        mime,
        storage_path: storagePath,
        bytes: file.size,
        taken_at: form.get("taken_at") || null,
        caption: form.get("caption") || null,
      })
      .select("id")
      .single();
    if (insErr) return json(500, { error: `media insert: ${insErr.message}` });
    mediaId = row.id;
  }

  // --- 5. attach to playlist (creates it if needed); trigger bumps the frames
  const { error: plErr } = await admin.rpc("add_media_to_playlist", { p_media: mediaId, p_slug: playlist });
  if (plErr) return json(500, { error: `playlist: ${plErr.message}` });

  await admin.from("upload_tokens").update({ last_used: new Date().toISOString() }).eq("id", tok.id);

  return json(200, { media_id: mediaId, playlist, sha256: sha, deduped: Boolean(existing) });
});
