# 19 Bot

A moderation and utility Discord application for The Submission Server. Every
command is a slash command. All state lives in one SQLite database behind a
single async connection with versioned migrations.

---

## Contents

- [Setup](#setup)
- [Running](#running)
- [Commands](#commands)
- [Who can run what](#who-can-run-what)
- [Automatic behaviour](#automatic-behaviour)
- [Blog and RSS monitoring](#blog-and-rss-monitoring)
- [Channel archiving](#channel-archiving)
- [Configuration](#configuration)
- [Database](#database)
- [Development](#development)
- [Verification and legal](#verification-and-legal)
- [Project structure](#project-structure)
- [Adding a cog](#adding-a-cog)

---

## Setup

1. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
2. Install dependencies:

```bash
pip install -r requirements-dev.txt
```

Every Discord ID has a working default in `core/config.py`, so the bot runs
without a fully populated `.env`. Any value can be overridden by environment
variable. Configuration is validated at startup and the bot refuses to start on
a missing token, a zero ID, two IDs that collide, or a non-HTTPS policy URL.

### Required Discord setup

The bot needs both privileged intents enabled in the Developer Portal:

| Intent | Why it is required |
|---|---|
| **Server Members** | `on_member_join` drives bot kicks, after-ban re-bans, account flagging, and the New Member role |
| **Message Content** | Link blocking must read the text of messages the bot was not mentioned in |

Neither is optional. The bot will start without them, but the join pipeline and
link blocking will silently do nothing. See
[`docs/VERIFICATION.md`](docs/VERIFICATION.md) for the review justification and
the minimum permission set.

## Running

**Windows, with auto-restart:**

```bat
start_bot.bat
```

Exits without restarting on exit code 2 (a configuration or token failure),
since restarting cannot fix those.

**Directly:**

```bash
python main.py
```

### Docker

The image is multi-stage: dependencies are built in a stage that carries a C
toolchain, and only the resulting virtualenv is copied into the runtime image.
No compiler and no package manager ship. There are no additional services to
run, because all state is local SQLite.

It scans clean: **0 critical, 0 high, 0 medium, 0 low** on `docker scout`, down
from 84 findings. See [`docs/SECURITY.md`](docs/SECURITY.md) for what changed
and how to keep it that way.

**With compose (recommended):**

```bash
docker compose up -d --build
```

**Plain docker:**

```bash
docker build -t 19bot:latest .
```

```bash
docker run -d --name 19bot --restart unless-stopped --init --env-file .env -v "$(pwd)/databases:/app/databases" -v "$(pwd)/logs:/app/logs" -v "$(pwd)/archives:/app/archives" 19bot:latest
```

**Status and logs:**

```bash
docker compose ps
```

```bash
docker compose logs -f bot
```

```bash
docker compose down
```

| Aspect | Behaviour |
|---|---|
| Base | `python:3.13-alpine`, built for linux/amd64 and linux/arm64 |
| Size | ~151 MB, 54 packages |
| Vulnerabilities | 0 critical, 0 high, 0 medium, 0 low |
| User | `botuser`, uid 10001, never root |
| Ports | None. The bot is an outbound gateway client and listens for nothing |
| Services | None. SQLite only, so no database or cache container |
| Persistence | `databases/`, `logs/` and `archives/` are bind-mounted |
| Health | From `logs/heartbeat`, written every 60s; tolerance 180s |
| Shutdown | `init: true` plus SIGTERM handling; `docker stop` takes ~1s and exits 0 |
| Config | Entirely from the environment via `--env-file .env`; nothing baked in |

`docker compose ps` shows `health: starting` for the first minute, then
`healthy` once the event loop has written its first heartbeat. Heartbeat
freshness is the liveness signal rather than log activity: a quiet bot
legitimately writes no logs for hours, which previously reported unhealthy and
could drive a restart loop.

Startup takes roughly a minute to reach "Logged in" on a large guild, because
the members intent chunks the member list before `on_ready`.

#### Git Bash on Windows

`$(pwd)` in the `docker run` command above works in a POSIX shell. Under Git
Bash on Windows, MSYS rewrites the path and Docker silently mounts a different,
empty directory instead, so the bot appears to start with no data. Either use
`docker compose`, which resolves relative paths itself, or prefix the command:

```bash
MSYS_NO_PATHCONV=1 docker run ...
```

#### Bind mounts on native Linux

The container runs as uid 10001 and bind mounts keep host ownership, so on
native Linux the mounted directories must be writable by that uid. Either chown
them once:

```bash
sudo chown -R 10001:10001 databases logs archives
```

Or run the container as yourself:

```bash
DOCKER_USER="$(id -u):$(id -g)" docker compose up -d
```

Docker Desktop on Windows and macOS maps ownership permissively and needs
neither.

**macOS and Linux, with auto-restart:**

```bash
./start_bot.sh
```

Same exit-code contract as `start_bot.bat`: code 2 is a configuration failure
and stops rather than looping.

---

## Commands

14 commands in total. "Response" describes who sees the bot's reply.

### Moderation

| Command | Who can run it | Response | What it does |
|---|---|---|---|
| `/ptimeout <member> [reason]` | Moderators | Public | Times the member out for 28 days and records them so the timeout auto-renews indefinitely |
| `/undoptimeout <member>` | Moderators | Public | Removes the member from the renewal list and lifts any active timeout |
| `/listptimeout` | Moderators | Private | Lists everyone currently on the renewal list, with reason and date |

`/ptimeout` refuses, with an explanation, when the target is yourself, a bot, the
server owner, someone whose highest role is at or above yours, someone above the
bot's own highest role, or **anyone with Administrator** — Discord exempts
administrators from timeouts entirely, so the call would otherwise report success
while doing nothing.

### Security

| Command | Who can run it | Response | What it does |
|---|---|---|---|
| `/botwhitelist add <bot_id>` | Moderators | Public | Permits a bot to remain in the server |
| `/botwhitelist remove <bot_id>` | Moderators | Public | Revokes a bot's permission; it is kicked if it rejoins |
| `/botwhitelist list` | Moderators | Private | Lists every permitted bot, resolving each ID to a name |

`bot_id` accepts a raw ID or a `<@123>` style mention, and rejects anything that
is not a plausible snowflake.

### Live events

| Command | Who can run it | Response | What it does |
|---|---|---|---|
| `/addevent` | Moderators | Private | Opens a form for title and description, then a dropdown of the server's real voice and stage channels, then optionally attaches an image |
| `/listevents` | Moderators | Private | Lists all active events with ID, host, and channel |
| `/deleteevent <event_id>` | Moderators | Public | Deactivates an event by ID |

`/deleteevent` is a soft delete: the record is retained with `is_active = 0` for
auditing. Event drafts expire 10 minutes after the channel is chosen.

### General

| Command | Who can run it | Response | What it does |
|---|---|---|---|
| `/debate` | Everyone, 3 per minute | Picker private, diagram public | Dropdown of reference diagrams; the chosen one is posted into the channel |
| `/rules` | Everyone, 3 per 5 minutes | Public | Posts the server rules link |
| `/privacy` | Everyone | Private | What data the bot stores and how to have it removed |
| `/commands19` | Moderators | Private | This command list, in-client |

### Owner only

| Command | Who can run it | Response | What it does |
|---|---|---|---|
| `/archive [channel] [include_attachments] [include_threads] [restart]` | The owner account only | Private ack, then DM | Exports a channel's entire history to local disk |

See [Channel archiving](#channel-archiving).

## Who can run what

Three tiers, and they are not the same as Discord's permission tiers.

| Tier | Means | Commands |
|---|---|---|
| **Everyone** | Any member | `/debate`, `/rules`, `/privacy` |
| **Moderators** | The Moderator role, the Admin role, the Administrator permission, or the server owner | All moderation, security, and event commands, plus `/commands19` |
| **Owner** | The single account in `OWNER_USER_ID` | `/archive` |

Every gated command is enforced **twice**:

1. `default_permissions` greys the command out in the Discord client. This is
   only a hint — Discord still lets anyone holding the underlying permission bit
   invoke the command, and administrators bypass it entirely.
2. A server-side check in `core/checks.py` decides whether the command actually
   runs. This is what enforces the tier.

That distinction matters most for `/archive`: a server administrator is **not**
the app owner and cannot run it, even though `default_permissions` alone would
let them try.

---

## Automatic behaviour

### When someone joins

`cogs/membership.py` owns the only `on_member_join` listener and runs these steps
in a fixed order, stopping as soon as the member has been removed:

1. **Unauthorised bot?** Kicked, logged to staff-logs. Nothing else applies.
2. **On the after-ban list?** Re-banned. The list entry is only cleared once the
   ban actually succeeds.
3. **Account under 24 hours old?** Flagged in the database, given the Unverified
   role, and reported to staff-logs. The member still continues to steps 4 and 5.
4. **New Member role** assigned, with a 24-hour expiry recorded.
5. **Welcome DM**, followed by the security notice DM if enabled.

### Background tasks

| Task | Interval | Purpose |
|---|---|---|
| `expire_new_member_roles` | 10 minutes | Removes the New Member role once its 24 hours are up |
| `refresh_perpetual_timeouts` | 1 hour | Tops up timeouts within 2 days of their 28-day limit, and prunes rows for members who left |
| `check_feeds` | 5 minutes | Polls the RSS feeds and announces new posts |
| `prune_pending` | 5 minutes | Drops abandoned event drafts |
| `daily_reminder` | 19:19 EST | Iron Dome reminder in mod-chat |

Every task waits for the gateway to be ready, starts in `cog_load` (never before
migrations have run), and has an error handler. Without one, `tasks.loop` stops
permanently on the first unhandled exception.

### Link blocking

Members holding the New Member role cannot post links. Detection covers schemed
URLs, `www.` prefixes, Discord invites, and bare hostnames on common TLDs.
Moderators are exempt even if they hold the role, and the warning message deletes
itself after 30 seconds.

Message text is read in memory for this check and **never written to disk**.

---

## Blog and RSS monitoring

Five feeds are polled every 5 minutes. New posts go to the content channel,
except Reddit which has its own channel and pings a notification role.

| Feed | Source | Destination | Status |
|---|---|---|---|
| Quran Talk Blog | `qurantalkblog.com/feed/` | Content channel | Verified working |
| Hadith Critic Blog | `hadithcriticblog.com/rss.xml` | Content channel | **URL corrected** |
| Submission to God | `submissiontogod.wordpress.com/feed/` | Content channel | Verified working |
| EJ's Medium Blog | `medium.com/feed/@dutchkickboxing1on1` | Content channel | Verified working |
| Hadith Criticism Reddit | `reddit.com/r/HadithCriticism/.rss` | Reddit channel, pings role | Working, rate limited |

All five were checked live on 27 August 2026.

**The Hadith Critic Blog feed was broken.** The site moved off WordPress, so
`/feed/` returns 404 and had been failing silently. The working feed is
`/rss.xml`, which serves 81 entries. Because the site also changed its URL
scheme, the last-announced link is no longer present in the feed, so the first
poll after this fix announces only the single newest post rather than replaying
the backlog.

**Reddit rate limits aggressively.** It returns `x-ratelimit-remaining: 0` even
on a successful response, so occasional 429s are normal. A 429 now triggers a
per-feed backoff that honours `Retry-After`, doubles on repeat, and caps at one
hour, instead of retrying every 5 minutes.

### Checking feed health

A feed whose URL moves fails quietly: the bot logs a warning and skips it.
Whenever a blog stops announcing, run:

```bash
python scripts/check_feeds.py
```

It reports each feed's status and entry count, and for a failing feed it probes
common alternative paths and suggests a working replacement URL.

Feed behaviour:

- Only entries newer than the last announced link are posted, oldest first.
  Multiple posts inside one 5-minute window are all announced, not just the
  newest.
- At most 3 announcements per feed per poll, so a feed that rewrites its
  permalinks cannot flood a channel.
- A feed seen for the first time records a baseline and announces nothing.
- If the stored link vanishes from the feed window, only the newest entry is
  announced.

---

## Channel archiving

`/archive` exports a channel's full history — message text, authors, timestamps,
embeds, reactions, and attached files — to local disk.

**Access.** Restricted to the single account in `OWNER_USER_ID`. Hidden from
everyone else in the client, and the identity check runs server-side because
Discord lets guild administrators invoke commands regardless of
`default_permissions`.

**No channel output.** The command acknowledges privately and posts nothing in
the channel. A Discord interaction token expires after 15 minutes, far less than
a large channel takes, so the work detaches into a background task and all
progress arrives as edits to a single DM. The DM channel is opened *before* any
work begins: if your DMs are closed the command refuses immediately rather than
starting a run with nowhere to report.

**Options.**

| Option | Default | Effect |
|---|---|---|
| `channel` | Current channel | Which channel to archive |
| `include_attachments` | Yes | Download images and files, not just text |
| `include_threads` | Yes | Also archive threads inside the channel |
| `restart` | No | Ignore the previous run and start from the beginning |

**Output.**

```
archives/<guild_id>/<channel>-<channel_id>/
├── manifest.json          # counts, timings, errors, resume point
└── <channel>/
    ├── messages.jsonl     # one JSON record per message
    └── attachments/       # <message_id>-<attachment_id>-<filename>
```

**Behaviour.** Runs are resumable: the last archived message ID is stored, so
re-running continues from there. Forum and media channels hold no messages of
their own, so those are archived by walking their active and archived threads.
Attachments stream to disk with bounded concurrency and are skipped above
`ARCHIVE_ATTACHMENT_MAX_MB`. Both View Channel and Read Message History are
checked up front, because without the latter Discord returns an empty list
rather than an error, which would look like an empty channel.

Archives contain member data. They are gitignored, excluded from the Docker
image, and covered by the [Privacy Policy](docs/PRIVACY_POLICY.md).

---

## Configuration

All settings are environment variables with defaults in `core/config.py`. See
`.env.example` for the full list. The ones most worth knowing:

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | — | Required |
| `OWNER_USER_ID` | The owner's ID | Who may run `/archive` |
| `GLOBAL_COMMAND_SYNC` | `0` | Sync commands globally instead of to one guild |
| `LOG_LEVEL` | `INFO` | Root log level |
| `SECURITY_NOTICE_ENABLED` | `1` | Set to `0` to stop DMing the security notice to new members |
| `NEW_MEMBER_DURATION_HOURS` | `24` | How long the New Member role lasts |
| `SUSPICIOUS_ACCOUNT_MAX_AGE_HOURS` | `24` | Account age below which a join is flagged |
| `ARCHIVE_ATTACHMENT_MAX_MB` | `100` | Skip attachments larger than this |
| `LEGAL_SITE_BASE_URL` | `https://hadithcritic.github.io/19--Bot` | Base of the published policy site; both policy URLs derive from it |
| `TERMS_OF_SERVICE_URL` | Derived from the base | Override individually if needed |
| `PRIVACY_POLICY_URL` | Derived from the base | Override individually if needed |

---

## Database

One file, `databases/bot.db`. Schema changes are applied through the `MIGRATIONS`
tuple in `core/database.py`; each migration runs once inside a transaction and is
recorded in `schema_migrations`.

| Table | Holds |
|---|---|
| `whitelist` | Bot IDs permitted in the server |
| `after_ban_users` | Users to re-ban on rejoin |
| `perpetual_timeouts` | Users whose timeout auto-renews |
| `new_member_roles` | New Member role expiry timers |
| `suspicious_accounts` | Accounts flagged as newly created |
| `custom_live_events` | Staff-created events |
| `blog_state` | Last announced entry per feed |
| `archive_runs` | Archive progress and resume points |
| `command_sync_state` | Fingerprint of the last synced command set |
| `schema_migrations` | Which migrations have been applied |

The connection uses WAL and a 10-second busy timeout, and is shared process-wide
so concurrent writes from the background tasks queue instead of colliding.

`vc_joins` is retained as a read-only archive of historical voice activity.
Nothing writes to it and no command reads it; drop it when the history is no
longer wanted.

To add a schema change, append a new `(version, description, statements)` entry
to `MIGRATIONS`. Statements may be bare SQL or `(sql, params)` pairs. Never edit
an existing migration — deployed databases have already recorded it as applied.

### Command syncing

Commands sync only when their definitions actually change, verified by hashing
the payloads. Discord allows 200 command creates per day per guild and a bulk
overwrite counts against that, so an unconditional sync on every startup could
exhaust the budget during a crash-restart loop and leave the server with no
commands. Guild-scoped syncs also apply instantly, unlike global ones.

---

## Development

```bash
ruff check . && ruff format --check . && pytest
```

CI runs the same three commands on every push, plus a check that no `.env` or
`.db` file is tracked in git.

Dependencies are pinned exactly in `requirements.txt` and `requirements-dev.txt`,
including ruff. An unpinned linter means CI can fail on rules that do not exist
in the version installed locally.

### Uniformity across machines

One Python version, declared once in `.python-version` and read by everything
else: the Dockerfile build arg, the compose build arg, `requires-python`, ruff's
`target-version`, Pyright, and both workflows. `tests/test_deployment.py`
asserts they still agree, so they cannot drift.

`.gitattributes` normalises line endings to LF on checkout, with `.bat` files
kept as CRLF because cmd.exe requires it. Without that, a file committed from
Windows carries CRLF and a shell script fails inside the Linux container with
"bad interpreter".

252 tests, none of which need a Discord connection. Coverage is concentrated on
the logic where a regression is most costly: configuration validation, database
migrations against realistic legacy schemas, permission resolution, the
member-join ordering, feed diffing and backoff, and archive path safety.

---

## Verification and legal

Terms of Service and a Privacy Policy are required for Discord app verification,
and the Developer Terms of Service require the privacy policy to be publicly
hosted and reachable from the app itself, which `/privacy` provides.

- [`docs/TERMS_OF_SERVICE.md`](docs/TERMS_OF_SERVICE.md)
- [`docs/PRIVACY_POLICY.md`](docs/PRIVACY_POLICY.md)
- [`docs/VERIFICATION.md`](docs/VERIFICATION.md) — status of all six criteria,
  Developer Portal steps, the minimum permission set, and privileged intent
  justifications
- [`docs/SECURITY.md`](docs/SECURITY.md) — container vulnerability baseline,
  dependency floors, and the rescan command

Discord fetches both URLs and rejects any it cannot reach, so the markdown is
rendered to real web pages in `site/` and served by GitHub Pages:

```bash
python scripts/build_legal_site.py          # regenerate site/
python scripts/build_legal_site.py --check  # CI guard against drift
```

The markdown stays the single source of truth; CI fails if `site/` is stale.

### What is safe to publish

`databases/` and `archives/` hold live member data and are **deny-by-default**
in `.gitignore`: everything is ignored, and only two files are allowed back.

| Path | Contents | Published? |
|---|---|---|
| `databases/bot.db` | 1,640 distinct user IDs, 1,757 usernames, ban reasons, 11 months of voice activity | **Never** |
| `databases/blog_data.json` | Public blog URLs only, but runtime state | No |
| `databases/debate_image_map.json` | Filename to label mapping | Yes, safe |
| `databases/.gitkeep` | Empty placeholder | Yes |
| `archives/**` | Message text, authors, timestamps, attachments | **Never** |
| `logs/**` | Operational logs | Never |
| `.env` | Bot token and API keys | **Never** |

An extension list such as `databases/*.db` would miss a `.sqlite`, `.csv`, or
`.jsonl` export dropped in later, which is why the rule ignores the folder and
allowlists the exceptions instead.

### Before making the repository public

```bash
python scripts/preflight_public.py
```

Checks what git would actually publish: tracked paths against a list of things
that must never be committed, tracked file contents for credential shapes, and
reachable history for anything sensitive. Exit code 0 means safe. See
[`docs/VERIFICATION.md`](docs/VERIFICATION.md) for the full publishing steps.

---

## Project structure

```
19Embed/
├── main.py                   # Entry: logging, config, database, cogs, command sync
│
├── core/
│   ├── config.py             # Typed, environment-backed, validated configuration
│   ├── database.py           # One aiosqlite connection, WAL, versioned migrations
│   ├── logging_setup.py      # Rotating file handler plus UTF-8 console handler
│   ├── checks.py             # staff_only / admin_only / owner_only
│   └── errors.py             # Narrow error types and the shared interaction responder
│
├── cogs/
│   ├── membership.py         # The only on_member_join listener; orders the join pipeline
│   ├── security.py           # Bot whitelist, account flagging, link blocking
│   ├── moderation.py         # Perpetual timeouts, after-ban, New Member role lifecycle
│   ├── events.py             # Live events
│   ├── admin.py              # RSS monitoring, daily reminder, /rules, /privacy
│   ├── debate.py             # /debate diagram picker
│   └── archive.py            # Owner-only channel export
│
├── .python-version           # The single source of the supported Python version
├── docs/                     # Terms, privacy, verification, security baseline
├── site/                     # Generated public policy pages (GitHub Pages)
├── scripts/
│   ├── check_feeds.py        # Feed health check
│   ├── build_legal_site.py   # Renders docs/*.md into site/
│   └── preflight_public.py   # Pre-publication secret scan
├── tests/                    # pytest suite, no Discord connection required
│
├── databases/                # SQLite database and the debate label map (gitignored)
├── logs/                     # Rotating logs (gitignored)
├── archives/                 # Channel exports (gitignored)
└── resources/Images/         # Debate diagrams
```

---

## Adding a cog

1. Create `cogs/new_cog.py` with an `async def setup(bot)` function.
2. Read shared state from `bot.db`; add any new IDs to `core/config.py`.
3. Gate commands with `@staff_only()`, `@admin_only()`, or `@owner_only()` from
   `core.checks`. Never rely on `default_permissions` alone.
4. Start any `tasks.loop` in `cog_load`, cancel it in `cog_unload`, and give it
   both a `before_loop` and an `.error` handler.
5. Add the module to `EXTENSIONS` in `main.py`.

A cog that fails to load aborts startup: running with moderation features
silently disabled is worse than not starting.
