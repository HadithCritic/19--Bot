# Container Security

Baseline from `docker scout`, what was changed to reach it, and what remains.

---

## Result

| | Before | After |
|---|---|---|
| Critical | 2 | **0** |
| High | 11 | **0** |
| Medium | 23 | **0** |
| Low | 44 | **0** |
| Unspecified | 4 | **0** |
| **Total** | **84** | **0** |
| Packages | 162 | 54 |
| Image size | 262 MB | 151 MB |

`docker scout cves --only-fixed` reports **no vulnerable packages**.

Reproduce with:

```bash
docker build -t 19bot:latest .
```

```bash
docker scout cves 19bot:latest
```

---

## What was changed, and why

### 1. Dependency floors

Four of the six runtime dependencies were behind. `aiohttp` alone accounted for
33 findings.

| Package | Was | Now | Cleared |
|---|---|---|---|
| `aiohttp` | 3.10.11 | **3.14.3** | 2 High, 16 Medium, 15 Low |
| `python-dotenv` | 1.0.1 | **1.2.3** | 1 Medium (link following) |
| `aiosqlite` | 0.20.0 | 0.22.1 | none outstanding |
| `feedparser` | 6.0.11 | 6.0.14 | none outstanding |
| `tzdata` | 2024.2 | 2026.3 | none outstanding |

`discord.py` requires `aiohttp<4,>=3.7.4`, so 3.14.3 is inside its supported
range. **Do not lower `aiohttp` below 3.14.3 or `python-dotenv` below 1.2.2**;
`tests/test_deployment.py` fails if you do.

Upgrading aiohttp exposed a latent shutdown deadlock. See
[Shutdown](#shutdown-regression-found-while-doing-this) below.

### 2. No package manager in the runtime image

Three High findings were not in the application's dependencies at all:

- `setuptools` 70.3.0 (path traversal) came from the wheel bundled in
  `ensurepip`.
- `msgpack` 1.1.2 (use-after-free, ×2) is vendored inside `pip`.

Neither is importable by the bot. Both the venv's and the base image's
`pip`/`setuptools`/`ensurepip` are removed, and the build asserts the app still
imports afterwards. A runtime image has no business shipping a package manager.

Only creating new virtualenvs inside the container stops working, which nothing
does. If you ever need pip in there, use the builder stage.

### 3. Alpine instead of Debian slim

This is the change that removed the last two Criticals.

Debian marks `perl` **`Essential: yes, Priority: required`**, so it cannot be
removed with `apt`. It carried:

| CVE | Severity | Fixed upstream |
|---|---|---|
| CVE-2026-13221 | Critical | No |
| CVE-2026-12087 | Critical | No |
| CVE-2026-48959 | High | No |
| CVE-2026-48962 | High | No |

Four permanent findings, no fix available, in a package nothing here invokes.
Alpine has no perl at all, and dropped the package count from 142 to 54.

Verified before switching, because a base-image change is not cosmetic:

- `musllinux` wheels exist for `aiohttp` and `discord.py`, so nothing compiles
- `zoneinfo.ZoneInfo("America/New_York")` resolves, which the scheduled tasks need
- The bot reaches `Logged in` over the gateway, so musl's DNS and TLS are fine
- All eight migrations apply on a bind mount and writes persist
- The healthcheck, heartbeat and graceful stop all behave as on Debian

### 4. OS patches at build time

The published base image lags its own security updates by days, which is where
every fixable `openssl` advisory came from (3.5.6 → 3.5.7 cleared four Highs on
Debian). The runtime stage now runs `apk upgrade --no-cache`.

This trades byte-for-byte reproducibility for being patched. For a long-running
service that is the right way round. If you need bit-identical rebuilds, pin the
base image by digest and accept a manual patch cadence instead.

---

## Shutdown regression found while doing this

Upgrading aiohttp turned a latent race into a reliable hang: `docker stop` took
the full 30-second grace period and the container was `SIGKILL`ed with exit 137,
losing the clean database close.

The cause was two concurrent shutdowns. The SIGTERM handler started
`bot.close()` as a task while `main()`'s `finally` block called `bot.close()`
again; each blocked before logging anything, so the symptom was total silence
after "Received SIGTERM".

Shutdown now has one owner: the handler only sets an `asyncio.Event`, `main()`
races that against the run task, and the whole sequence is bounded
(`_SHUTDOWN_TIMEOUT`, kept under the compose `stop_grace_period`). The database
handle is closed in `finally` regardless of whether the Discord client
cooperates. `docker stop` is now 1 second, exit 0.

This was not caused by Alpine. It was reproduced on Debian slim with the same
dependency versions before the base image changed.

---

## Remaining risk

`docker scout` reports nothing outstanding. That is a point-in-time statement,
not a guarantee:

- New advisories are published against pinned versions continuously. Rescan
  before each deploy.
- The scan covers OS and Python packages. It says nothing about this
  application's own logic.
- `apk upgrade` at build time means a rebuild is required to pick up patches.
  An image built months ago is stale however clean it scanned on the day.

### Keeping it clean

```bash
docker build -t 19bot:latest . && docker scout cves --only-fixed 19bot:latest
```

If that reports anything, raise the pin in `requirements.txt`, rebuild and
re-run the test suite. `docker scout recommendations 19bot:latest` covers base
image moves.

`scripts/preflight_public.py` is a separate concern: it checks that no secret or
member data is about to be published, not that dependencies are patched. Run
both.
