# Supabase, Explained for the Drive

*A narrated walkthrough of Supabase — what it is, the parts frame-sync uses, and how to set it all up. Written to be listened to. About thirty minutes at normal speed.*

## Chapter 1. What Supabase is

Supabase is a Postgres platform. That one sentence is most of what you need. Every Supabase project is a real, dedicated PostgreSQL database, and everything else — authentication, file storage, realtime messaging, serverless functions — is built around that database rather than beside it.

The company was founded in 2020 by Paul Copplestone and Ant Wilson, came out of Y Combinator, and has grown fast. In June 2026 it raised a five hundred million dollar Series F at a ten and a half billion dollar valuation, led by GIC. In early October 2026 it announced another hundred and fifty million and acquired Turso, the SQLite company. Supabase reports more than nine million developers and more than a quarter million customers, and says most new databases are now created by AI agents and AI-driven tools. That last fact matters for a support role: a growing share of the people filing tickets didn't write the code they're asking about.

The pitch that made Supabase popular is "the open source Firebase alternative." But the deeper idea is: don't build yet another proprietary thing. Take the best existing open source tools and wire them together. Postgres for the database. PostgREST to turn a schema into a REST API. GoTrue, now called Supabase Auth, for login. Phoenix and Elixir for realtime. Deno for functions. Almost all of it is Apache 2 licensed, and the whole stack runs on your laptop with one command.

If you remember one phrase for the interview, it's this: Supabase's job is to make Postgres do everything, and your job as a support engineer is to know where Postgres ends and the Supabase layer begins. Most tickets live right on that seam.

## Chapter 2. The product surface

Let's walk the product surface once, top to bottom, so every name is familiar.

The database. A dedicated Postgres instance per project, with full SQL, direct connections, and a long list of extensions you can turn on from the dashboard.

The Data API. PostgREST reads your schema and exposes every table as a REST endpoint automatically. Insert a table, get an API. There's a GraphQL endpoint too, from the pg_graphql extension. You can turn the Data API off entirely and use Supabase as plain hosted Postgres.

Auth. Email and password, magic links, one-time codes, social logins, SAML single sign-on, multi-factor, anonymous users, and recently a full OAuth 2.1 server. Users live in a schema called auth, in a table called auth dot users, which you never expose directly. Every signed-in request carries a JWT, and that JWT is what the database uses to decide what the user can see.

Storage. Object storage for files, with the clever twist that access control is written as Postgres row level security policies on a table called storage dot objects. There are three ways in: a normal HTTP API, resumable uploads using the TUS protocol for big files, and a full S3-compatible API. There's a CDN in front of it and on-the-fly image transformations on paid plans.

Realtime. A WebSocket service written in Elixir with three features. Broadcast is pub/sub — send a message on a topic, everyone subscribed gets it. Presence tracks who's online, using CRDTs. And Postgres Changes streams database inserts, updates and deletes to subscribers over logical replication. We'll spend real time on realtime later because frame-sync depends on it and because the recommended approach changed.

Edge Functions. TypeScript functions running on a Deno-compatible runtime, deployed globally. Two seconds of CPU per request, a few minutes of wall clock, two hundred fifty six megabytes of memory.

Then the Postgres-native extras. Vectors through pgvector, for AI embeddings. Cron through pg_cron, for scheduled jobs. Queues through pgmq. Vault for encrypted secrets inside the database. And branching, which gives each pull request its own database.

Around all of that: the Dashboard, also called Studio, which is open source and runs locally too. A SQL editor with an AI assistant. A logs explorer backed by Logflare. Security and performance advisors that lint your schema and will, for example, flag any public table with row level security turned off. And an MCP server so AI coding agents can manage projects directly — the local stack now exposes one too.

That's the whole surface. Frame-sync touches six of these: Postgres and row level security, Auth, Storage, Realtime, Edge Functions, and the API keys that tie them together. The second half of this recording goes deep on exactly those six.

## Chapter 3. Pricing in two minutes

You'll get billing questions in support, so here's the shape of it.

The Free plan is zero dollars. Two active projects, a five hundred megabyte database, one gigabyte of file storage, fifty thousand monthly active users, two hundred realtime connections, half a million edge function invocations. The catch everyone hits: free projects pause after one week of inactivity. "My project is paused" is a ticket you will answer a lot.

Pro starts at twenty five dollars a month per organization, and here's the subtlety. That twenty five is an organization fee. Compute is billed separately, per project, by the hour. Pro includes a ten dollar compute credit, which covers exactly one Micro instance. So one project is twenty five dollars. Two Micro projects is thirty five. Pro also raises the limits: eight gigabytes of database, a hundred gigabytes of storage, a hundred thousand monthly active users, five hundred realtime connections, image transformations, and daily backups kept for seven days.

Team is from five hundred ninety nine a month. It does not buy more capacity. It buys compliance: SOC 2, ISO 27001, a HIPAA add-on, dashboard SSO, read-only roles, longer log retention. Enterprise is custom.

Compute sizes run from Micro at ten dollars, one gigabyte of RAM, up through Small, Medium, Large — where you get your first dedicated CPUs — and on to Sixteen XL at about thirty seven hundred a month with two hundred fifty six gigabytes of RAM. Connection limits scale with compute. Micro allows sixty direct connections and two hundred through the pooler. Keep that number in your head; it explains a whole category of tickets.

A few add-ons matter for support. Point-in-time recovery is a hundred dollars a month per seven days of retention. An IPv4 address is about four dollars a month. Read replicas cost the same as the primary and need at least a Small instance. And the spend cap: Pro has it on by default, and it protects you from usage overages — but it does not cover compute hours or IPv4 hours. That's a billing surprise you'll explain more than once.

## Chapter 4. API keys and JWTs — the thing that changed

Now the deep dives. First, keys, because this is the biggest change in 2025 and 2026 and it generates confusion every single day.

Historically a Supabase project had two keys, both long-lived JWTs that started with E-Y-J. The anon key was the low-privilege one you shipped in browsers. The service role key bypassed all security and lived only on servers.

That's being replaced. The new keys are short strings, not JWTs. A publishable key starts with S-B underscore publishable. A secret key starts with S-B underscore secret. The publishable key is safe in browsers, mobile apps and source code — it acts as the anon role, or as the authenticated role once a user signs in. The secret key maps to the service role, which bypasses row level security. You can create several secret keys and revoke them one at a time, which you could never do with the old service role key without rotating the whole project's JWT secret and logging everyone out. And if a secret key ever shows up in a browser, Supabase detects it from the user agent and refuses it with a 401.

The timeline: Supabase says it's deprecating the anon and service role keys by the end of 2026. Creating the new keys does not revoke the old ones. Both work until you disable the legacy keys yourself, in settings, under API keys. The removal date is still listed as to be confirmed, so don't state a hard date to a customer.

The practical rule for headers. API keys go in a header called apikey. A signed-in user's JWT goes in the Authorization header as a Bearer token. Don't send a publishable or secret key as a Bearer token. That mix-up is behind most 401 tickets right now.

Alongside the key change came JWT signing keys. Instead of one shared secret, projects can use asymmetric keys — ES256 by default — with the public half published at a well-known JWKS endpoint. That means any service can verify a Supabase JWT locally without the secret, and keys can be rotated without downtime. In the JavaScript client, get claims verifies tokens that way.

One wrinkle you should know cold: Edge Functions have a setting called verify JWT, on by default, which checks the Authorization header before your code runs. Two official pages currently disagree on whether that check accepts the new non-JWT keys. The safe pattern, and what frame-sync does, is: for a function called by something that isn't a signed-in user — a webhook, a cron, an iPhone Shortcut — turn verify JWT off and do your own authorization in code. Frame-sync's ingest function checks its own upload token, hashed, against a table. That's a design decision you can defend in the interview.

## Chapter 5. Row level security — where most tickets live

Row level security, or RLS, is the heart of the Supabase security model. Here's how to think about it.

When you enable RLS on a table, the default becomes deny. Nobody sees anything until a policy says they can. A policy is a small SQL predicate attached to a table for one command — select, insert, update or delete — and for one role, usually authenticated or anon. A using clause filters which existing rows are visible. A with check clause validates new rows on insert and update. Policies for the same command are OR'd together.

The magic ingredient is auth dot uid. It reads the user's ID out of the JWT claims that PostgREST sets on every request. So a policy like "using owner_id equals auth uid" means every user automatically sees only their own rows, and the application code never has to filter. In frame-sync, each picture frame is an auth user, so auth uid is the device. The frame can read exactly the playlists it's assigned, the media in those playlists, and its own row in the devices table. It can update one thing: its own status. That's the whole authorization story, in one migration file, enforced by the database rather than by Python.

Now, the pitfalls. These are the tickets.

First, grants come before RLS. Postgres checks table grants first, then policies. A missing grant gives "permission denied." A policy that matches nothing gives an empty result. They look different, and knowing which one you're seeing saves an hour.

Second, views bypass RLS. A view runs as its owner, which is usually the postgres superuser. So a view over a protected table leaks everything unless you create it with security invoker equals true. The security advisor flags this.

Third, the SQL editor runs as postgres, which bypasses RLS. Testing a policy there proves nothing. Simulate a user instead: set local role authenticated, then set the request dot jwt dot claims setting to a JSON object with the user's sub. Frame-sync's test suite does exactly that for the frame role, the owner role and the service role — twenty two assertions.

Fourth, performance. Wrap auth uid in a subselect: open paren, select auth dot uid, close paren. That makes Postgres evaluate it once as an initplan instead of once per row. Supabase's own numbers: a hundred seventy nine milliseconds down to nine. Index every column a policy filters on. Avoid joins inside policies on hot tables. Always specify to authenticated or to anon so policies aren't evaluated for roles they don't apply to. And a subtle one: a subquery inside a policy is itself subject to RLS for the calling user, which frame-sync's storage policy relies on.

Fifth, the quickstart trap: a policy of using true, copied from a tutorial, is RLS turned off with extra steps. And the big one: a secret key in a client bundle. That's not an RLS problem, it's game over, and the fix is rotation.

## Chapter 6. Auth, for a device

Auth in frame-sync is deliberately boring. Each frame is an ordinary email and password user, created by the owner, with sign-ups disabled project-wide so nobody else can register. The Pi signs in with its email and password, gets an access token that lasts an hour by default and a refresh token, and refreshes before expiry.

Why do it this way instead of custom device tokens? Because then every other Supabase feature — RLS, storage policies, realtime authorization — already understands who the frame is. No custom auth layer, no service key on the device.

Two things to know for general support. Users are created from the dashboard under Authentication, Users, Add user, or from server code with the admin API and a secret key. And the auth dot users table is private; the convention is a public profiles table keyed on the user's ID, filled by a security definer trigger when a user is created. Never base authorization on user metadata, because users can edit their own metadata. Use app metadata or your own tables.

## Chapter 7. Storage, for photos

Storage is where the photos live. One private bucket, called media. Objects are keyed by owner ID, then a slash, then the SHA-256 hash of the file. The hash in the path gives you free deduplication: upload the same photo twice and the Edge Function notices before it writes anything.

Access is two RLS policies on storage dot objects. Owners can read and write anything under their own folder — there's a helper, storage dot foldername, that splits the path so you can compare the first segment to auth uid. The common mistake is a path prefix like uploads slash user ID, which puts "uploads" in position one instead of the user ID. Frames can read any object that a media row they're allowed to see points at — and that's where the subquery-inside-a-policy trick does the work.

The Pi never gets a storage credential. It asks for signed URLs — there's a batch call, create signed URLs, that signs a whole list at once with a one-hour expiry — and downloads from those.

For general support: public buckets serve files to anyone at a predictable URL. Private buckets need a JWT or a signed URL. Free allows fifty megabyte files; paid plans go to five hundred gigabytes. Resumable uploads are recommended above six megabytes, use the TUS protocol, and the chunk size must be exactly six megabytes. Image transformations — resize, crop, quality, format — are Pro and up, with a hundred origin images included. Frame-sync resizes on the phone instead, which keeps the project on the free tier.

## Chapter 8. Realtime — the part that changed under your feet

This is the one to really understand, because the recommended approach is different from what most tutorials show.

There are three realtime features. Presence, for who's online. Broadcast, which is plain pub/sub on named topics. And Postgres Changes, which streams row changes from the database to subscribers.

Postgres Changes is what everyone learns first, and Supabase now says, in its own docs, that Broadcast from Database is the recommended method for scalability and security. Why? Three reasons.

One: Postgres Changes authorizes every event against every subscriber. One row change with a hundred subscribers is a hundred RLS checks.

Two: it's processed on a single thread to preserve ordering, so a bigger compute instance doesn't make it faster. Above roughly three thousand concurrent subscribers on the same changes, you're stuck.

Three: deletes. RLS isn't applied to delete events, and the old row only contains the primary key unless you set replica identity full. And the table has to be added to a publication called supabase realtime, or nothing happens at all. "Realtime isn't delivering" tickets are almost always one of those.

Broadcast from Database flips the model. Instead of the realtime server watching your tables, your database decides what to send. A trigger calls a function — realtime dot send — with a payload, an event name, a topic, and a private flag. That inserts a row into realtime dot messages, and the realtime server picks it up from the write-ahead log. Authorization is a normal RLS policy on realtime dot messages, evaluated once when a client joins the channel, not per message. A helper called realtime dot topic returns the channel name so a policy can say "you may listen on this topic if you own it."

Here's frame-sync's version. Every frame has a manifest version number on its row in the devices table. Any change that could affect what a frame should display — a photo added to a playlist, a playlist assigned, the active playlist switched — runs through triggers that bump that number. A second trigger, after update on devices, fires only when manifest version actually changed, and calls realtime dot send on a topic named device colon the frame's ID, with the new version number as the payload. The frame's heartbeat writes don't bump the version, so they don't echo back.

The Pi subscribes to its own topic as a private channel. Private is the important word: the classic bug is a database broadcast that never arrives because the client didn't set private true. The client has to call set auth with the user's JWT first, and there has to be a select policy on realtime dot messages, or you get "you do not have permissions to read from this topic." Frame-sync's policy says: the device itself can listen on its own topic, and the owner can listen on any of their frames' topics.

A few gotchas worth knowing. Messages in realtime dot messages are kept in daily partitions and dropped after about seventy two hours. Realtime dot send doesn't create the day's partition — a client connecting does. So if you call send before any client has connected that day, you get a warning and the message is lost. JWTs expire after an hour, and the realtime socket closes when they do, so a long-lived client has to refresh and re-auth; frame-sync does it at twenty five minutes. And in the Python realtime client there's an open bug — issue sixteen fifty five on supabase-py — where set auth updates live channels but a reconnect replays the old token, so private channels silently die after an hour. The workaround is to resubscribe after refreshing. The frame-sync agent tears down and rebuilds the channel on any disconnect, and falls back to a five-minute poll regardless, so a missed message costs at most five minutes.

Quotas: Free gets two hundred peak connections and two million messages a month. Pro, five hundred connections and five million messages, with overage pricing after that.

## Chapter 9. Edge Functions, for the phone

The ingest Edge Function is the only privileged code in frame-sync. It's about a hundred lines of TypeScript running on Deno.

What it does, in order. Reads the Authorization header and hashes the bearer token. Looks that hash up in an upload tokens table using a secret-key client — this is the one place the service role is used. If there's no match, 401. Parses the multipart form: a file, a playlist slug, optionally a taken-at date. Validates the slug against a pattern and the file against a size cap and a MIME allow-list. Hashes the file bytes. Checks whether that owner already has media with that hash — if so, it skips the upload and reuses the row. Otherwise, uploads the object first, then inserts the media row, in that order, because a row without an object is worse than an object without a row. Finally it calls a database function, add media to playlist, which creates the playlist if needed and inserts the item. That insert fires the trigger that bumps every assigned frame and broadcasts. The function returns the media ID and whether it was deduplicated.

The iPhone side is a Shortcut. Pick photos, share, choose a playlist. The Shortcut resizes to two thousand forty eight pixels on the long edge and converts HEIC to JPEG on the phone, then posts the form to the function with the upload token. No app to build.

Why a token and not Supabase Auth on the phone? Shortcuts can't run an OAuth flow and have nowhere safe to keep a refresh token. A scoped, revocable, hashed bearer token checked by an Edge Function is the honest answer for that client. Revoke it by deleting a row.

For support: limits are two seconds of CPU per request, a hundred fifty seconds wall clock on Free and four hundred on paid, twenty megabyte bundles from the CLI, five from the dashboard. CPU exhaustion shows up as 546 errors. Secrets are set with supabase secrets set and take effect immediately, and secret names can't start with SUPABASE underscore. Functions see the project URL and keys as environment variables automatically. And the new key dictionaries — SUPABASE SECRET KEYS as JSON — are there alongside the legacy variables.

## Chapter 10. Connection pooling

One more topic before setup, because it's the top connectivity ticket.

There are three ways to connect. The direct connection, on port 5432 at db dot your ref dot supabase dot co, is IPv6 only unless you buy the IPv4 add-on. So "can't connect from my VPS" is very often an IPv4-only host. The shared pooler, Supavisor, has an IPv4 address and two modes on two ports. Port 5432 is session mode: one client, one backend, for long-lived servers. Port 6543 is transaction mode: the backend is returned after every transaction, which is what serverless and edge need — but it means no prepared statements, no session-level SET, no advisory locks across transactions. Since February 2025, 6543 is transaction mode only. Prisma users need pgbouncer equals true on the pooled URL and a direct URL for migrations.

The symptom table: "remaining connection slots are reserved" means the compute tier's limit is exhausted — usually a Lambda or edge function opening a client per invocation. "Max clients reached in session mode" means switch to transaction mode or shrink pools. Look at pg stat activity grouped by application name to find the culprit; set application name in every client so you can.

## Chapter 11. Setting it up — local first

Now the walkthrough. Everything here runs on big-sparky before touching the cloud.

Step one, install the CLI. On Linux the simplest route is Homebrew, which works on Linux too: brew install supabase slash tap slash supabase. Or grab the dot deb or dot rpm from the GitHub releases page for the supabase CLI. The CLI needs Docker or a compatible runtime — Docker Desktop, Podman, or colima. On Bluefin, podman is already there; you can also run the local stack inside your Distrobox if you'd rather keep it off the host.

Step two, start the stack. In the frame-sync repo there's already a supabase folder with a config file, a migrations folder and a seed file, so you skip supabase init. Run supabase start. The first run pulls a dozen images: Postgres, GoTrue for auth, PostgREST, the realtime server, the storage API with imgproxy, Kong as the gateway, Mailpit for catching emails, Studio, the edge runtime, Logflare, Vector, and Supavisor. When it finishes it prints a table. The project URL is localhost port 54321. Studio is on 54323. The database is on 54322 with user postgres, password postgres. And it prints keys — current CLIs print a publishable and a secret key; older ones print anon and service role. Either works with the code.

Step three, load the schema. Run supabase db reset. That applies every file in supabase slash migrations in order, then runs seed dot SQL. The migration creates the tables, the RLS policies, the triggers, the broadcast function, the manifest and heartbeat RPCs, and the media bucket with its storage policies. The seed creates an owner user, two frame users — frame kitchen and frame office — two playlists, the device assignments, and a dev upload token. Open Studio in a browser and look at the tables; look at Authentication to see the three users.

Step four, start the ingest function locally. Run supabase functions serve ingest dash dash no verify JWT. It watches the file and reloads on changes.

Step five, run the agent as a frame. In the repo, make a virtual environment and pip install dash R requirements. Then set environment variables: BACKEND equals supabase, SUPABASE URL equals the local URL, SUPABASE ANON KEY equals the publishable or anon key the CLI printed, DEVICE EMAIL equals frame dash kitchen at frames dot local, DEVICE PASSWORD equals frame dash kitchen dash pass, MEDIA ROOT equals a frames folder, STATE DIR equals a state folder. Run python dash M agent dot main. You should see it sign in, fetch an empty manifest, open the realtime channel, and start heartbeating.

Step six, simulate the phone. Curl a JPEG to the function: dash F file equals at some JPEG, dash F playlist equals family, with a header Authorization Bearer dev dash upload dash token, to localhost 54321 slash functions slash v1 slash ingest. The function returns a media ID. In the agent's log you should see "broadcast: manifest" with a new version, then a plus sign and the file landing in frames slash family. Send the same file again and the function returns deduped true, and nothing else happens. That's the whole loop, end to end, on one machine.

If you don't want Docker at all, there's a shortcut for the database half: tests slash run supabase SQL dot SH loads the migration and seed onto any plain Postgres using a small shim for the auth, realtime and storage schemas, and runs the twenty two assertions.

## Chapter 12. Setting it up — hosted

Moving to the cloud is five steps.

One. At supabase dot com slash dashboard, create a project. Pick your organization and the Free plan, name it, choose a region near Austin — US East is fine — and set a strong database password. Save that password; you need it for direct connections and for linking the CLI.

Two. Get the URL and keys. Click Connect, or go to Settings, API Keys. Copy the project URL and the publishable key for the Pi. If the project only shows legacy keys, use the anon key; if it shows both, prefer the new ones. Don't copy the secret key anywhere except the Edge Function's environment, which Supabase handles for you.

Three. Push the schema. In the repo: supabase login, then supabase link dash dash project ref and your project's reference, which prompts for the database password. Then supabase db push. That applies the migration to the hosted database. Hosted projects don't let you insert into auth dot users directly, so instead of running the seed, create the three users in the dashboard under Authentication, Users, Add user — the owner, frame kitchen and frame office — with auto-confirm on. Then run the second half of the seed's DO block in the SQL editor with the real user IDs pasted in, to create the playlists, devices, assignments and upload token. And turn off "allow new users to sign up" under Authentication, Sign In Providers.

Four. Deploy the function. Supabase functions deploy ingest dash dash no verify JWT. The secret key is injected automatically; there are no other secrets to set.

Five. Point things at it. On the Pi, the env file gets the hosted URL, the publishable key, and the frame's email and password; the systemd unit in the repo starts the agent on boot and restarts it on failure. In the Shortcut, change the URL to your project's functions endpoint and generate a real upload token — a long random string — insert its SHA-256 hash into upload tokens from the SQL editor, and paste the plaintext into the Shortcut's Authorization header.

Then open the Security Advisor under Database and read every warning. Anything it flags on this schema, you should be able to explain; that's the best five-minute interview prep in the whole project.

## Chapter 13. The talking points

Let's close with how this maps onto the interview.

The one-sentence version of the project: each frame is an auth user, so row level security is the entire authorization story; the Pi holds only a public key and its own login; one RPC builds the manifest; signed URLs serve the photos; and a trigger bumps a version and broadcasts it on a private channel so the frame never polls.

The design decisions you can defend. Why Broadcast from Database instead of Postgres Changes. Why an upload token instead of Supabase Auth on the phone. Why hash in the storage path. Why the heartbeat doesn't bump the version. Why the frame's storage access rides on RLS inside a policy subquery. And what you'd change at ten thousand frames: denormalize owner ID onto child tables so policies stop joining, move heartbeats off the hot devices table, and fan out realtime through a service instead of one channel per device.

The support instincts. Grants before policies. Views bypass RLS. Wrap auth uid in a select. apikey header for keys, Bearer for user tokens. Port 5432 for servers, 6543 for serverless. Private true on database broadcasts. Check the publication before debugging Postgres Changes. And when a Free project is paused and the customer is upset: empathy, the facts, the setting that prevents it — and don't promise credits.

That's Supabase. Drive safe.
