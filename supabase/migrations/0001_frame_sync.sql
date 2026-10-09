-- frame-sync : Supabase schema
-- Apply with:  supabase db push   (or paste into the SQL editor)
--
-- Design in one paragraph (say this out loud in the interview):
--   Every frame is an ordinary auth user, so auth.uid() identifies the device
--   and RLS does all the authorization — the Pi only ever holds the public anon
--   key plus its own session. Playlists/media belong to a human owner; a frame
--   can read exactly the rows the manifest needs and update exactly one row
--   (its own status). A trigger bumps devices.manifest_version whenever anything
--   the frame cares about changes, and a second trigger broadcasts that bump on
--   a private Realtime channel the frame is listening to. No service key leaves
--   the server; the Edge Function that ingests photos is the only privileged code.

-- ---------------------------------------------------------------------------
-- 0. helper: current owner id for the phone/web user
-- ---------------------------------------------------------------------------
create schema if not exists frame;

-- ---------------------------------------------------------------------------
-- 1. tables
-- ---------------------------------------------------------------------------
create table public.media (
  id           uuid primary key default gen_random_uuid(),
  owner_id     uuid not null references auth.users(id) on delete cascade,
  sha256       text not null,
  filename     text not null,
  mime         text not null check (mime in ('image/jpeg','image/png','image/webp','video/mp4')),
  storage_path text not null,                       -- object key in the "media" bucket
  bytes        bigint,
  width        int,
  height       int,
  taken_at     timestamptz,
  uploaded_at  timestamptz not null default now(),
  tags         text[] not null default '{}',
  caption      text,
  unique (owner_id, sha256)                           -- dedupe re-uploads per owner
);
create index media_owner_recent on public.media (owner_id, taken_at desc nulls last);
create index media_tags on public.media using gin (tags);

create table public.playlists (
  id         uuid primary key default gen_random_uuid(),
  owner_id   uuid not null references auth.users(id) on delete cascade,
  slug       text not null check (slug ~ '^[a-z0-9-]{1,40}$'),  -- folder name on the Pi
  name       text not null,
  updated_at timestamptz not null default now(),
  unique (owner_id, slug)
);

-- Relational, not embedded: a photo can be in many playlists; ordering is a column.
create table public.playlist_items (
  playlist_id uuid not null references public.playlists(id) on delete cascade,
  media_id    uuid not null references public.media(id) on delete cascade,
  position    int  not null default 0,
  added_at    timestamptz not null default now(),
  primary key (playlist_id, media_id)
);
create index playlist_items_media on public.playlist_items (media_id);

create table public.devices (
  id               uuid primary key references auth.users(id) on delete cascade, -- the frame's auth user
  owner_id         uuid not null references auth.users(id) on delete cascade,
  name             text not null,                                                -- "frame-kitchen"
  width            int  not null default 1920,
  active_playlist  uuid references public.playlists(id) on delete set null,
  manifest_version bigint not null default 1,
  status           jsonb not null default '{}'::jsonb,                           -- heartbeat
  seen_at          timestamptz,
  created_at       timestamptz not null default now(),
  unique (owner_id, name)
);

create table public.device_playlists (
  device_id   uuid not null references public.devices(id) on delete cascade,
  playlist_id uuid not null references public.playlists(id) on delete cascade,
  primary key (device_id, playlist_id)
);
create index device_playlists_playlist on public.device_playlists (playlist_id);

-- Long-lived upload tokens for the iPhone Shortcut (hash only; see Edge Function).
create table public.upload_tokens (
  id         uuid primary key default gen_random_uuid(),
  owner_id   uuid not null references auth.users(id) on delete cascade,
  token_hash text not null unique,
  label      text,
  created_at timestamptz not null default now(),
  last_used  timestamptz
);

-- ---------------------------------------------------------------------------
-- 2. helpers
-- ---------------------------------------------------------------------------
-- Is the caller a frame? (devices.id = auth.uid())
create or replace function frame.is_device() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.devices d where d.id = auth.uid());
$$;

-- Owner of the calling frame (null if caller isn't a frame)
create or replace function frame.device_owner() returns uuid
language sql stable security definer set search_path = public as $$
  select d.owner_id from public.devices d where d.id = auth.uid();
$$;

-- ---------------------------------------------------------------------------
-- 3. manifest_version bumping
--    Any change that can alter what a frame should display bumps every affected
--    device. Centralised here so no client has to remember to do it.
-- ---------------------------------------------------------------------------
create or replace function frame.bump_devices_for_playlist(p uuid) returns void
language sql security definer set search_path = public as $$
  update public.devices d
     set manifest_version = manifest_version + 1
   where d.id in (select device_id from public.device_playlists where playlist_id = p)
      or d.active_playlist = p;
$$;

create or replace function frame.on_playlist_items_change() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  perform frame.bump_devices_for_playlist(coalesce(new.playlist_id, old.playlist_id));
  update public.playlists set updated_at = now() where id = coalesce(new.playlist_id, old.playlist_id);
  return null;
end $$;

create trigger playlist_items_bump
after insert or update or delete on public.playlist_items
for each row execute function frame.on_playlist_items_change();

create or replace function frame.on_device_playlists_change() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  update public.devices set manifest_version = manifest_version + 1
   where id = coalesce(new.device_id, old.device_id);
  return null;
end $$;

create trigger device_playlists_bump
after insert or delete on public.device_playlists
for each row execute function frame.on_device_playlists_change();

-- active_playlist / width changed by the phone → bump (but NOT on heartbeat writes)
create or replace function frame.on_device_update() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.active_playlist is distinct from old.active_playlist
     or new.width is distinct from old.width then
    new.manifest_version := old.manifest_version + 1;
  end if;
  return new;
end $$;

create trigger devices_self_bump
before update on public.devices
for each row execute function frame.on_device_update();

-- media row edited (e.g. re-processed variant) → bump every frame showing it
create or replace function frame.on_media_change() returns trigger
language plpgsql security definer set search_path = public as $$
declare p uuid;
begin
  for p in select playlist_id from public.playlist_items where media_id = coalesce(new.id, old.id) loop
    perform frame.bump_devices_for_playlist(p);
  end loop;
  return null;
end $$;

create trigger media_bump
after update of storage_path, filename or delete on public.media
for each row execute function frame.on_media_change();

-- ---------------------------------------------------------------------------
-- 4. Realtime: Broadcast from Database (recommended over postgres_changes)
--    Topic per device: "device:<uuid>". Only fire when manifest_version moved,
--    so the frame's own heartbeat writes don't echo back to it.
-- ---------------------------------------------------------------------------
create or replace function frame.broadcast_manifest_bump() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.manifest_version is distinct from old.manifest_version then
    -- realtime.send(payload, event, topic, private). broadcast_changes() would
    -- ship whole NEW/OLD rows; we only want the version number on the wire.
    perform realtime.send(
      jsonb_build_object('id', new.id, 'manifest_version', new.manifest_version),
      'manifest',
      'device:' || new.id::text,
      true
    );
  end if;
  return null;
end $$;

create trigger devices_broadcast
after update on public.devices
for each row execute function frame.broadcast_manifest_bump();

-- Who may *receive* on a private channel: the device itself, or its owner.
create policy "device or owner can listen"
on realtime.messages for select to authenticated
using (
  realtime.topic() = 'device:' || auth.uid()::text
  or exists (
    select 1 from public.devices d
     where 'device:' || d.id::text = realtime.topic()
       and d.owner_id = auth.uid()
  )
);

-- ---------------------------------------------------------------------------
-- 5. RLS — deny by default, then open exactly what each role needs
-- ---------------------------------------------------------------------------
alter table public.media            enable row level security;
alter table public.playlists        enable row level security;
alter table public.playlist_items   enable row level security;
alter table public.devices          enable row level security;
alter table public.device_playlists enable row level security;
alter table public.upload_tokens    enable row level security;

-- Owners: full control of their own rows
create policy owner_all on public.media
  for all to authenticated using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy owner_all on public.playlists
  for all to authenticated using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy owner_all on public.playlist_items
  for all to authenticated
  using (exists (select 1 from public.playlists p where p.id = playlist_id and p.owner_id = auth.uid()))
  with check (exists (select 1 from public.playlists p where p.id = playlist_id and p.owner_id = auth.uid()));
create policy owner_all on public.devices
  for all to authenticated using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy owner_all on public.device_playlists
  for all to authenticated
  using (exists (select 1 from public.devices d where d.id = device_id and d.owner_id = auth.uid()))
  with check (exists (select 1 from public.devices d where d.id = device_id and d.owner_id = auth.uid()));
create policy owner_all on public.upload_tokens
  for all to authenticated using (owner_id = auth.uid()) with check (owner_id = auth.uid());

-- Frames: read-only on what they're assigned, write-only on their own status
create policy device_read_self on public.devices
  for select to authenticated using (id = auth.uid());
create policy device_heartbeat on public.devices
  for update to authenticated using (id = auth.uid()) with check (id = auth.uid());
-- NB: a frame *can* in principle update its own active_playlist via this policy.
-- Acceptable for a hobby build; the strict version is a column-level grant:
--   revoke update on public.devices from authenticated;
--   grant  update (status, seen_at) on public.devices to authenticated;
-- (plus owners going through an RPC). Good interview tangent.

create policy device_read_assignments on public.device_playlists
  for select to authenticated using (device_id = auth.uid());
create policy device_read_playlists on public.playlists
  for select to authenticated
  using (exists (select 1 from public.device_playlists dp where dp.playlist_id = id and dp.device_id = auth.uid()));
create policy device_read_items on public.playlist_items
  for select to authenticated
  using (exists (select 1 from public.device_playlists dp where dp.playlist_id = playlist_id and dp.device_id = auth.uid()));
create policy device_read_media on public.media
  for select to authenticated
  using (exists (
    select 1 from public.playlist_items pi
    join public.device_playlists dp on dp.playlist_id = pi.playlist_id
    where pi.media_id = id and dp.device_id = auth.uid()));

-- ---------------------------------------------------------------------------
-- 6. The manifest, as one RPC. Runs as the caller, so RLS applies — a frame
--    can only ever get its own manifest. Returns JSON the agent maps 1:1.
-- ---------------------------------------------------------------------------
create or replace function public.get_manifest() returns jsonb
language sql stable security invoker set search_path = public as $$
  select jsonb_build_object(
    'device_id',        d.id,
    'name',             d.name,
    'manifest_version', d.manifest_version,
    'width',            d.width,
    'active_playlist',  (select slug from public.playlists where id = d.active_playlist),
    'items', coalesce((
      select jsonb_agg(jsonb_build_object(
               'media_id',     m.id,
               'playlist',     p.slug,
               'filename',     m.filename,
               'storage_path', m.storage_path,
               'sha256',       m.sha256,
               'bytes',        m.bytes
             ) order by p.slug, pi.position, pi.added_at)
        from public.device_playlists dp
        join public.playlists      p  on p.id = dp.playlist_id
        join public.playlist_items pi on pi.playlist_id = p.id
        join public.media          m  on m.id = pi.media_id
       where dp.device_id = d.id
    ), '[]'::jsonb)
  )
  from public.devices d
  where d.id = auth.uid();
$$;
grant execute on function public.get_manifest() to authenticated;

-- Heartbeat RPC keeps the update surface tiny and typed.
create or replace function public.heartbeat(p_status jsonb) returns void
language sql security invoker set search_path = public as $$
  update public.devices set status = p_status, seen_at = now() where id = auth.uid();
$$;
grant execute on function public.heartbeat(jsonb) to authenticated;

-- ---------------------------------------------------------------------------
-- 7. Storage: one private bucket, objects keyed <owner_id>/<sha256>.<ext>
--    Owners read/write their prefix; frames read objects referenced by media
--    rows they can see (RLS on public.media does the heavy lifting).
-- ---------------------------------------------------------------------------
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('media', 'media', false, 26214400, array['image/jpeg','image/png','image/webp','video/mp4'])
on conflict (id) do nothing;

create policy "owner rw own prefix" on storage.objects
  for all to authenticated
  using  (bucket_id = 'media' and (storage.foldername(name))[1] = auth.uid()::text)
  with check (bucket_id = 'media' and (storage.foldername(name))[1] = auth.uid()::text);

create policy "device reads assigned media" on storage.objects
  for select to authenticated
  using (
    bucket_id = 'media'
    and exists (select 1 from public.media m where m.storage_path = name)  -- RLS on media filters to assigned
  );

-- ---------------------------------------------------------------------------
-- 8. Owner-side convenience: add a media row to a playlist by slug.
--    Used by the Edge Function (as service role) and by the phone UI.
-- ---------------------------------------------------------------------------
create or replace function public.add_media_to_playlist(p_media uuid, p_slug text) returns void
language plpgsql security invoker set search_path = public as $$
declare pid uuid;
begin
  select id into pid from public.playlists
   where owner_id = (select owner_id from public.media where id = p_media) and slug = p_slug;
  if pid is null then
    insert into public.playlists (owner_id, slug, name)
    values ((select owner_id from public.media where id = p_media), p_slug, initcap(replace(p_slug,'-',' ')))
    returning id into pid;
  end if;
  insert into public.playlist_items (playlist_id, media_id, position)
  values (pid, p_media, extract(epoch from now())::int)
  on conflict do nothing;
end $$;
grant execute on function public.add_media_to_playlist(uuid, text) to authenticated, service_role;
