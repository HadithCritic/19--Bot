"""Tests for the pure logic extracted out of the cogs."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from cogs.admin import Admin, select_new_entries
from cogs.debate import DiagramCatalog
from cogs.events import PendingEvent, PendingEventStore
from cogs.security import contains_link

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=UTC)


# --- Link detection (cogs/security.py) ---


@pytest.mark.parametrize(
    "content",
    [
        "check https://example.com",
        "http://example.com/path?x=1",
        "visit www.example.com now",
        "join discord.gg/abc123",
        "https://discord.com/invite/xyz",
        "look at example.com/page",
        "grifter.xyz",
        "MiXeD://nope but HTTPS://EXAMPLE.COM works",
    ],
)
def test_links_are_detected(content):
    assert contains_link(content)


@pytest.mark.parametrize(
    "content",
    [
        "",
        "just a normal message",
        "Quran 17:36 says to verify",
        "the file is config.py",
        "version 2.0 is out",
        "e.g. see the pinned message",
    ],
)
def test_non_links_are_not_flagged(content):
    assert not contains_link(content)


# --- Feed diffing (cogs/admin.py) ---


def entry(link):
    return {"link": link, "title": link}


def test_no_baseline_announces_nothing():
    assert select_new_entries([entry("a")], None) == []


def test_unchanged_feed_announces_nothing():
    assert select_new_entries([entry("a"), entry("b")], "a") == []


def test_single_new_entry_is_announced():
    assert select_new_entries([entry("b"), entry("a")], "a") == [entry("b")]


def test_multiple_new_entries_are_announced_oldest_first():
    """The old code only ever looked at entries[0], losing the older post."""
    feed = [entry("d"), entry("c"), entry("b"), entry("a")]
    assert select_new_entries(feed, "a") == [entry("b"), entry("c"), entry("d")]


def test_backlog_is_capped():
    feed = [entry(str(i)) for i in range(20, 0, -1)]
    assert len(select_new_entries(feed, "1")) == 3


def test_unknown_baseline_only_announces_the_newest():
    feed = [entry("z"), entry("y"), entry("x")]
    assert select_new_entries(feed, "gone-from-the-feed") == [entry("z")]


def test_empty_feed_is_safe():
    assert select_new_entries([], "a") == []


# --- Rules rate limit (cogs/admin.py) ---


def make_admin():
    return Admin(SimpleNamespace(db=None))  # type: ignore[arg-type]


def test_rules_allows_three_then_blocks():
    admin = make_admin()
    assert [admin._allow_rules_use(1) for _ in range(4)] == [True, True, True, False]


def test_rules_limit_is_per_user():
    admin = make_admin()
    for _ in range(3):
        admin._allow_rules_use(1)
    assert admin._allow_rules_use(2) is True


def test_rules_usage_dict_does_not_grow_without_bound():
    """The old dict kept an entry per user forever."""
    admin = make_admin()
    for user_id in range(2000):
        admin._allow_rules_use(user_id)
    assert len(admin._rules_usage) <= 500


# --- Pending event drafts (cogs/events.py) ---


def draft(created_at=NOW, channel_id=10):
    return PendingEvent(
        title="t",
        message="m",
        voice_channel_id=1,
        voice_channel_name="VC",
        guild_id=2,
        channel_id=channel_id,
        created_at=created_at,
    )


def test_draft_round_trips():
    store = PendingEventStore()
    store.put(1, draft())
    assert store.get(1, NOW) is not None


def test_draft_expires():
    store = PendingEventStore()
    store.put(1, draft())
    assert store.get(1, NOW + timedelta(minutes=11)) is None
    assert len(store) == 0


def test_put_evicts_drafts_that_have_already_expired():
    store = PendingEventStore()
    store.put(1, draft(created_at=NOW - timedelta(minutes=30)))
    store.put(2, draft(created_at=NOW))
    assert store.get(1, NOW) is None
    assert store.get(2, NOW) is not None


def test_prune_removes_only_expired_drafts():
    store = PendingEventStore()
    store.put(1, draft(created_at=NOW))
    store.put(2, draft(created_at=NOW + timedelta(minutes=9)))
    # Only draft 1 is past its 10 minute TTL at this point.
    assert store.prune(NOW + timedelta(minutes=11)) == 1
    assert store.get(2, NOW + timedelta(minutes=11)) is not None


def test_store_is_bounded():
    store = PendingEventStore()
    for user_id in range(60):
        store.put(user_id, draft(created_at=NOW + timedelta(seconds=user_id)))
    assert len(store) <= 25


def test_discard_is_safe_when_absent():
    PendingEventStore().discard(999)


# --- Diagram catalog (cogs/debate.py) ---


def test_catalog_includes_unmapped_images(tmp_path):
    """Nine images on disk were unreachable because they were not in the map."""
    folder = tmp_path / "img"
    folder.mkdir()
    (folder / "Mapped.png").write_bytes(b"x")
    (folder / "Unmapped.jpg").write_bytes(b"x")
    map_file = tmp_path / "map.json"
    map_file.write_text('{"Mapped.png": "Nice Label"}', encoding="utf-8")

    labels = {d.label for d in DiagramCatalog(str(folder), str(map_file)).scan()}
    assert labels == {"Nice Label", "Unmapped"}


def test_catalog_skips_empty_and_non_image_files(tmp_path):
    folder = tmp_path / "img"
    folder.mkdir()
    (folder / "good.png").write_bytes(b"x")
    (folder / "empty.png").write_bytes(b"")
    (folder / "NoExtension").write_bytes(b"x")
    (folder / "notes.txt").write_bytes(b"x")

    names = {d.path.name for d in DiagramCatalog(str(folder), str(tmp_path / "none.json")).scan()}
    assert names == {"good.png"}


def test_catalog_accepts_the_legacy_label_to_filename_map(tmp_path):
    folder = tmp_path / "img"
    folder.mkdir()
    (folder / "diagram.png").write_bytes(b"x")
    map_file = tmp_path / "map.json"
    map_file.write_text('{"Old Style Label": "diagram.png"}', encoding="utf-8")

    diagrams = DiagramCatalog(str(folder), str(map_file)).scan()
    assert [d.label for d in diagrams] == ["Old Style Label"]


def test_catalog_survives_a_corrupt_map(tmp_path):
    folder = tmp_path / "img"
    folder.mkdir()
    (folder / "a.png").write_bytes(b"x")
    map_file = tmp_path / "map.json"
    map_file.write_text("{not json", encoding="utf-8")

    assert len(DiagramCatalog(str(folder), str(map_file)).scan()) == 1


def test_catalog_handles_a_missing_folder(tmp_path):
    assert DiagramCatalog(str(tmp_path / "nope"), str(tmp_path / "m.json")).scan() == []


# --- Feed configuration integrity (no network) ---


def test_feed_ids_are_unique():
    from cogs.admin import _build_feeds

    ids = [f.feed_id for f in _build_feeds()]
    assert len(ids) == len(set(ids))


def test_every_feed_url_is_https():
    from cogs.admin import _build_feeds

    for feed in _build_feeds():
        assert feed.rss_url.startswith("https://"), feed.feed_id
        assert feed.url.startswith("https://"), feed.feed_id


def test_every_feed_routes_to_a_real_channel():
    from cogs.admin import _build_feeds

    for feed in _build_feeds():
        assert feed.channel_id > 0, feed.feed_id


def test_only_the_reddit_feed_pings_a_role():
    """A ping role on a high-volume feed would be noisy; keep it deliberate."""
    from cogs.admin import _build_feeds

    pinging = {f.feed_id for f in _build_feeds() if f.ping_role_id is not None}
    assert pinging == {"hadithcriticism_reddit"}


def test_feeds_have_names_and_emoji():
    from cogs.admin import _build_feeds

    for feed in _build_feeds():
        assert feed.name.strip(), feed.feed_id
        assert feed.emoji.strip(), feed.feed_id


# --- Feed rate-limit backoff (cogs/admin.py) ---


def test_backoff_uses_retry_after_when_given():
    from cogs.admin import _build_feeds

    admin = make_admin()
    feed = _build_feeds()[0]
    assert admin._note_rate_limit(feed, "60") == 60.0
    assert feed.feed_id in admin._feed_backoff


def test_backoff_falls_back_on_a_non_numeric_retry_after():
    from cogs.admin import _DEFAULT_BACKOFF_SECONDS, _build_feeds

    admin = make_admin()
    feed = _build_feeds()[0]
    # An HTTP-date rather than a number must not raise.
    assert admin._note_rate_limit(feed, "Wed, 21 Oct 2026 07:28:00 GMT") == (
        _DEFAULT_BACKOFF_SECONDS
    )


def test_backoff_defaults_when_header_is_absent():
    from cogs.admin import _DEFAULT_BACKOFF_SECONDS, _build_feeds

    admin = make_admin()
    assert admin._note_rate_limit(_build_feeds()[0], None) == _DEFAULT_BACKOFF_SECONDS


def test_repeat_rate_limits_escalate_then_cap():
    from cogs.admin import _MAX_BACKOFF_SECONDS, _build_feeds

    admin = make_admin()
    feed = _build_feeds()[0]
    first = admin._note_rate_limit(feed, "60")
    second = admin._note_rate_limit(feed, "60")
    assert second > first
    for _ in range(20):
        latest = admin._note_rate_limit(feed, "60")
    assert latest <= _MAX_BACKOFF_SECONDS


def test_backoff_is_per_feed():
    from cogs.admin import _build_feeds

    admin = make_admin()
    feeds = _build_feeds()
    admin._note_rate_limit(feeds[0], "60")
    assert feeds[0].feed_id in admin._feed_backoff
    assert feeds[1].feed_id not in admin._feed_backoff
