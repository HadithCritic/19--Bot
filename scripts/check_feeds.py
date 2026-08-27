"""Check every configured RSS feed and report which ones are healthy.

A feed whose URL moves fails silently: the bot logs a warning and skips it, and
nothing else breaks. That is how hadithcriticblog went months without posting.
Run this whenever a blog stops announcing.

    python scripts/check_feeds.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DISCORD_TOKEN", "not-needed-for-this-check")

import aiohttp
import feedparser

from cogs.admin import _FEED_HEADERS, _FEED_TIMEOUT, _build_feeds

# Common alternative paths to try when the configured URL fails.
_FALLBACK_PATHS = ("/rss.xml", "/feed/", "/feed", "/atom.xml", "/index.xml", "/rss")


async def probe(session: aiohttp.ClientSession, url: str) -> tuple[int, int, str]:
    """Return (http_status, entry_count, note) for one URL."""
    try:
        async with session.get(url, allow_redirects=True) as response:
            body = await response.text()
            if response.status != 200:
                return response.status, 0, ""
            parsed = feedparser.parse(body)
            note = ""
            if parsed.entries:
                newest = parsed.entries[0].get("title") or "?"
                note = str(newest)[:60]
            return 200, len(parsed.entries), note
    except aiohttp.ClientError as exc:
        return 0, 0, f"{type(exc).__name__}: {exc}"
    except TimeoutError:
        return 0, 0, "timeout"


async def main() -> int:
    feeds = _build_feeds()
    failures: list[str] = []

    async with aiohttp.ClientSession(timeout=_FEED_TIMEOUT, headers=_FEED_HEADERS) as session:
        for feed in feeds:
            status, count, note = await probe(session, feed.rss_url)

            if status == 200 and count > 0:
                print(f"OK       {feed.feed_id:<24} {count:>3} entries  {note}")
                continue

            reason = f"HTTP {status}" if status else note or "unreachable"
            if status == 200 and count == 0:
                reason = "reachable but zero entries"
            print(f"FAIL     {feed.feed_id:<24} {reason}")
            print(f"         configured: {feed.rss_url}")
            failures.append(feed.feed_id)

            # Suggest a working alternative so the fix is obvious.
            base = feed.url.rstrip("/")
            for path in _FALLBACK_PATHS:
                candidate = base + path
                if candidate == feed.rss_url:
                    continue
                alt_status, alt_count, alt_note = await probe(session, candidate)
                if alt_status == 200 and alt_count > 0:
                    print(f"         try instead: {candidate}  ({alt_count} entries, {alt_note})")
                    break

    print()
    if failures:
        print(f"{len(failures)} of {len(feeds)} feeds need attention: {', '.join(failures)}")
        return 1
    print(f"All {len(feeds)} feeds healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
