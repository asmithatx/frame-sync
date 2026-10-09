-- Minimal stand-in for the Supabase platform schemas so the migration can be
-- loaded and RLS/trigger behaviour tested on a plain Postgres. NOT for real use.
-- Mirrors: auth.users/identities, auth.uid(), realtime.messages/topic()/send(),
-- storage.buckets/objects/foldername(), and the anon/authenticated/service_role roles.
create extension if not exists pgcrypto;

do $$ begin
  if not exists (select 1 from pg_roles where rolname='anon')          then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname='service_role')  then create role service_role nologin bypassrls; end if;
end $$;

-- auth -----------------------------------------------------------------------
create schema if not exists auth;
create table auth.users (
  id uuid primary key, instance_id uuid, aud text, role text, email text unique,
  encrypted_password text, email_confirmed_at timestamptz,
  raw_app_meta_data jsonb, raw_user_meta_data jsonb, created_at timestamptz, updated_at timestamptz
);
create table auth.identities (
  id uuid primary key, user_id uuid references auth.users(id), provider_id text, provider text,
  identity_data jsonb, created_at timestamptz, updated_at timestamptz, last_sign_in_at timestamptz,
  unique (provider_id, provider)
);
create or replace function auth.uid() returns uuid language sql stable as $$
  select (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')::uuid
$$;

-- realtime -------------------------------------------------------------------
create schema if not exists realtime;
create table realtime.messages (
  id bigserial primary key, topic text, extension text, payload jsonb, event text,
  private boolean, inserted_at timestamptz default now()
);
alter table realtime.messages enable row level security;
create or replace function realtime.topic() returns text language sql stable as $$
  select nullif(current_setting('realtime.topic', true), '')
$$;
create or replace function realtime.send(payload jsonb, event text, topic text, private boolean default true)
returns void language sql as $$
  insert into realtime.messages (topic, extension, payload, event, private)
  values (topic, 'broadcast', payload, event, private)
$$;

-- storage --------------------------------------------------------------------
create schema if not exists storage;
create table storage.buckets (
  id text primary key, name text, public boolean default false,
  file_size_limit bigint, allowed_mime_types text[]
);
create table storage.objects (
  id uuid primary key default gen_random_uuid(), bucket_id text references storage.buckets(id),
  name text, owner uuid, created_at timestamptz default now()
);
alter table storage.objects enable row level security;
create or replace function storage.foldername(name text) returns text[] language plpgsql immutable as $$
declare parts text[];
begin
  parts := string_to_array(name, '/');
  return parts[1 : array_length(parts, 1) - 1];
end $$;

grant usage on schema public, auth, realtime, storage to anon, authenticated, service_role;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
grant select on realtime.messages to authenticated;
grant all on storage.objects, storage.buckets to authenticated, service_role;
