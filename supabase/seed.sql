-- Dev seed. Runs on `supabase db reset` against the LOCAL stack.
-- Creates one owner and two frames as auth users (password login), two playlists,
-- and an upload token for the iPhone Shortcut.
--
-- Owner:   andrew@frames.local / frames-dev-pass
-- Frames:  frame-kitchen@frames.local / frame-kitchen-pass
--          frame-office@frames.local  / frame-office-pass
-- Upload token (plaintext, dev only): dev-upload-token
--
-- On hosted Supabase create the users in Dashboard → Auth (or via the admin API
-- from the Edge Function), then run the DO block below with real UUIDs.

create extension if not exists pgcrypto;

do $$
declare
  v_owner   uuid := '00000000-0000-0000-0000-00000000a001';
  kitchen   uuid := '00000000-0000-0000-0000-00000000f001';
  office    uuid := '00000000-0000-0000-0000-00000000f002';
  pl_family uuid;
  pl_art    uuid;
  u record;
begin
  -- auth users (local stack lets us write auth.users directly; hosted does not)
  for u in select * from (values
      (v_owner, 'andrew@frames.local',        'frames-dev-pass'),
      (kitchen,  'frame-kitchen@frames.local', 'frame-kitchen-pass'),
      (office,   'frame-office@frames.local',  'frame-office-pass')
    ) as t(id, email, pw)
  loop
    insert into auth.users (id, instance_id, aud, role, email, encrypted_password,
                            email_confirmed_at, raw_app_meta_data, raw_user_meta_data,
                            created_at, updated_at)
    values (u.id, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
            u.email, crypt(u.pw, gen_salt('bf')), now(),
            '{"provider":"email","providers":["email"]}'::jsonb, '{}'::jsonb, now(), now())
    on conflict (id) do nothing;
    insert into auth.identities (id, user_id, provider_id, provider, identity_data, created_at, updated_at, last_sign_in_at)
    values (gen_random_uuid(), u.id, u.id::text, 'email',
            jsonb_build_object('sub', u.id::text, 'email', u.email), now(), now(), now())
    on conflict do nothing;
  end loop;

  insert into public.playlists (owner_id, slug, name) values (v_owner, 'family', 'Family')
    on conflict (owner_id, slug) do update set name = excluded.name returning id into pl_family;
  insert into public.playlists (owner_id, slug, name) values (v_owner, 'ai-art', 'AI Art')
    on conflict (owner_id, slug) do update set name = excluded.name returning id into pl_art;

  insert into public.devices (id, owner_id, name, active_playlist)
  values (kitchen, v_owner, 'frame-kitchen', pl_family),
         (office,  v_owner, 'frame-office',  pl_art)
  on conflict (id) do nothing;

  insert into public.device_playlists values (kitchen, pl_family), (office, pl_family), (office, pl_art)
  on conflict do nothing;

  insert into public.upload_tokens (owner_id, token_hash, label)
  values (v_owner, encode(digest('dev-upload-token', 'sha256'), 'hex'), 'iPhone shortcut (dev)')
  on conflict (token_hash) do nothing;
end $$;
