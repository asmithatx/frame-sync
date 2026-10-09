-- Behavioural test of the migration on plain Postgres (after supabase_shim.sql + migration + seed).
--   psql -d fs -v ON_ERROR_STOP=1 -f tests/test_supabase_rls.sql
-- Each block impersonates a JWT the way PostgREST does: set role + request.jwt.claims.
\set ON_ERROR_STOP on
\set QUIET on
\pset format unaligned
\pset tuples_only on

-- helper: assert(cond, msg)
create or replace function pg_temp.assert(ok boolean, msg text) returns void language plpgsql as $$
begin if not ok then raise exception 'ASSERT FAILED: %', msg; end if; raise notice 'ok: %', msg; end $$;

-- snapshot versions after seed (assignments bump too, by design)
create temp table v0 as select name, manifest_version as v from public.devices;
create temp table m0 as select count(*) as n from realtime.messages where event='manifest';

-- ---------------------------------------------------------------- as service role: ingest a photo
set role service_role;
insert into public.media (owner_id, sha256, filename, mime, storage_path, bytes)
values ('00000000-0000-0000-0000-00000000a001', repeat('a',64), 'lake.jpg', 'image/jpeg',
        '00000000-0000-0000-0000-00000000a001/' || repeat('a',64) || '.jpg', 1000);
select public.add_media_to_playlist((select id from public.media where sha256 = repeat('a',64)), 'family');
reset role;

select pg_temp.assert((select d.manifest_version - v0.v from public.devices d join v0 using(name) where name='frame-kitchen') = 1, 'ingest bumped kitchen by 1');
select pg_temp.assert((select d.manifest_version - v0.v from public.devices d join v0 using(name) where name='frame-office')  = 1, 'ingest bumped office by 1');
select pg_temp.assert((select count(*) from realtime.messages where event='manifest') - (select n from m0) = 2, 'ingest sent exactly two broadcasts');
create temp table v1 as select name, manifest_version as v from public.devices; grant select on v0, v1, m0 to authenticated;
create temp table m1 as select topic, count(*) as n from realtime.messages where event='manifest' group by topic;
grant select on m1 to authenticated;

-- ---------------------------------------------------------------- as frame-kitchen
set role authenticated;
set request.jwt.claims = '{"sub":"00000000-0000-0000-0000-00000000f001","role":"authenticated"}';

select pg_temp.assert((select count(*) from public.devices) = 1, 'frame sees only itself');
select pg_temp.assert((select count(*) from public.playlists) = 1, 'frame sees only assigned playlist');
select pg_temp.assert((select count(*) from public.media) = 1, 'frame sees only assigned media');
select pg_temp.assert((select count(*) from public.upload_tokens) = 0, 'frame cannot see upload tokens');
select pg_temp.assert((public.get_manifest()->>'manifest_version')::int = (select v from v1 where name='frame-kitchen'), 'manifest version via RPC matches row');
select pg_temp.assert(jsonb_array_length(public.get_manifest()->'items') = 1, 'manifest has 1 item');
select pg_temp.assert(public.get_manifest()->'items'->0->>'playlist' = 'family', 'item is in family');
select pg_temp.assert(public.get_manifest()->>'active_playlist' = 'family', 'active playlist resolved to slug');

-- heartbeat must NOT bump the version or broadcast
select public.heartbeat('{"files":1}'::jsonb);
select pg_temp.assert((select manifest_version from public.devices) = (select v from v1 where name='frame-kitchen'), 'heartbeat did not bump version');
select pg_temp.assert((select status->>'files' from public.devices) = '1', 'heartbeat stored');

-- frame cannot write media or playlists
do $$ begin
  begin
    insert into public.media (owner_id, sha256, filename, mime, storage_path)
    values ('00000000-0000-0000-0000-00000000a001', repeat('b',64), 'x', 'image/jpeg', 'x');
    raise exception 'ASSERT FAILED: frame inserted media';
  exception when insufficient_privilege then raise notice 'ok: frame cannot insert media'; end;
end $$;

-- storage: frame can read its assigned object, not others
set role service_role;
insert into storage.objects (bucket_id, name) values
  ('media', '00000000-0000-0000-0000-00000000a001/' || repeat('a',64) || '.jpg'),
  ('media', '00000000-0000-0000-0000-00000000a001/' || repeat('z',64) || '.jpg');
set role authenticated;
select pg_temp.assert((select count(*) from storage.objects) = 1, 'frame sees exactly its 1 assigned object (RLS applies inside policy subquery)');

-- realtime: frame may listen on its own topic only
set realtime.topic = 'device:00000000-0000-0000-0000-00000000f001';
select pg_temp.assert((select count(*) from realtime.messages) >= 1, 'frame can read its own topic');
set realtime.topic = 'device:00000000-0000-0000-0000-00000000f002';
select pg_temp.assert((select count(*) from realtime.messages) = 0, 'frame cannot read another frame topic');

-- ---------------------------------------------------------------- as owner
set request.jwt.claims = '{"sub":"00000000-0000-0000-0000-00000000a001","role":"authenticated"}';
select pg_temp.assert((select count(*) from public.devices) = 2, 'owner sees both frames');
set realtime.topic = 'device:00000000-0000-0000-0000-00000000f002';
select pg_temp.assert((select count(*) from realtime.messages) >= 1, 'owner can listen on any of their frames');

-- owner switches kitchen's active playlist → bump + broadcast
update public.devices set active_playlist = (select id from public.playlists where slug='ai-art') where name='frame-kitchen';
select pg_temp.assert((select d.manifest_version - v1.v from public.devices d join v1 using(name) where name='frame-kitchen') = 1, 'active_playlist change bumped kitchen by 1');
select pg_temp.assert((select count(*) from realtime.messages where topic='device:00000000-0000-0000-0000-00000000f001') - (select n from m1 where topic='device:00000000-0000-0000-0000-00000000f001') = 1, 'one more broadcast to kitchen');

-- owner removes the photo from family → both frames bump
delete from public.playlist_items where media_id = (select id from public.media where sha256 = repeat('a',64));
select pg_temp.assert((select d.manifest_version - v1.v from public.devices d join v1 using(name) where name='frame-office') = 1, 'delete bumped office by 1');
reset role;
reset request.jwt.claims;

-- the manifest plan, as a frame — paste into the prep doc
set role authenticated;
set request.jwt.claims = '{"sub":"00000000-0000-0000-0000-00000000f001","role":"authenticated"}';
\pset tuples_only off
explain (costs off) select public.get_manifest();
reset role;
