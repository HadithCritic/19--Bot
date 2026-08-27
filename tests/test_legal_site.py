"""Tests for the legal-site generator.

Discord rejects a Terms of Service or Privacy Policy URL it cannot fetch, so the
published pages have to be correct and in step with the markdown they come from.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_legal_site import (
    PAGES,
    build_all,
    render_inline,
    render_markdown,
    wrap_page,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent


# --- Inline conversion ---


def test_bold_and_italic():
    assert render_inline("**bold**") == "<strong>bold</strong>"
    assert render_inline("*slanted*") == "<em>slanted</em>"


def test_inline_code_is_escaped_and_not_reparsed():
    out = render_inline("`a <b> **c**`")
    assert out == "<code>a &lt;b&gt; **c**</code>"


def test_links_render():
    assert render_inline("[x](https://e.invalid)") == '<a href="https://e.invalid">x</a>'


def test_cross_document_links_point_at_published_pages():
    assert 'href="privacy.html"' in render_inline("[Privacy Policy](PRIVACY_POLICY.md)")
    assert 'href="terms.html"' in render_inline("[Terms](docs/TERMS_OF_SERVICE.md)")


def test_angle_bracket_urls_render_as_links():
    out = render_inline("see <https://e.invalid/x>")
    assert '<a href="https://e.invalid/x">https://e.invalid/x</a>' in out


def test_html_in_prose_is_escaped():
    """Prose must never be able to inject markup into the published page."""
    out = render_inline('<script>alert("x")</script>')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_ampersands_are_escaped():
    assert "&amp;" in render_inline("this & that")


# --- Block conversion ---


def test_headings_by_level():
    assert render_markdown("# A") == "<h1>A</h1>"
    assert render_markdown("### C") == "<h3>C</h3>"


def test_horizontal_rule():
    assert render_markdown("---") == "<hr>"


def test_paragraphs_are_separate():
    out = render_markdown("One line.\n\nTwo line.")
    assert out.count("<p>") == 2


def test_single_newline_joins_a_paragraph():
    out = render_markdown("alpha\nbeta")
    assert out == "<p>alpha beta</p>"


def test_two_trailing_spaces_is_a_hard_break():
    out = render_markdown("alpha  \nbeta")
    assert "<br>" in out
    assert out.count("<p>") == 1


def test_bullet_list():
    out = render_markdown("- one\n- two")
    assert out == "<ul><li>one</li><li>two</li></ul>"


def test_numbered_list():
    out = render_markdown("1. one\n2. two")
    assert out.startswith("<ol>")
    assert out.count("<li>") == 2


def test_indented_continuation_joins_the_previous_item():
    out = render_markdown("- one\n  continued\n- two")
    assert "one continued" in out
    assert out.count("<li>") == 2


def test_table_renders_with_header_and_body():
    out = render_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<thead>" in out and "<th>A</th>" in out
    assert "<td>1</td>" in out and "<td>2</td>" in out


def test_wide_tables_can_scroll():
    """A wide table must not make the page itself scroll sideways on mobile."""
    out = render_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
    assert 'class="table-scroll"' in out


def test_fenced_code_block_is_escaped():
    out = render_markdown("```\n<b>x</b>\n```")
    assert out == "<pre><code>&lt;b&gt;x&lt;/b&gt;</code></pre>"


def test_blockquote():
    out = render_markdown("> quoted")
    assert out == "<blockquote><p>quoted</p></blockquote>"


def test_heading_immediately_after_paragraph():
    out = render_markdown("text\n## Next")
    assert "<p>text</p>" in out
    assert "<h2>Next</h2>" in out


def test_empty_input_is_empty_output():
    assert render_markdown("") == ""


# --- Page assembly ---


def test_page_is_a_complete_html_document():
    page = wrap_page(title="T", body="<p>x</p>", active="index.html")
    assert page.startswith("<!DOCTYPE html>")
    assert '<html lang="en">' in page
    assert "<title>T — 19 Bot</title>" in page
    assert 'name="viewport"' in page


def test_page_is_self_contained():
    """No external requests: the page must render with no network."""
    page = wrap_page(title="T", body="<p>x</p>", active="index.html")
    for pattern in ("<script", 'src="http', 'href="http://', '<link rel="stylesheet"'):
        assert pattern not in page
    assert "<style>" in page


def test_page_supports_dark_mode():
    page = wrap_page(title="T", body="", active="index.html")
    assert "prefers-color-scheme: dark" in page


def test_nav_marks_the_active_page():
    page = wrap_page(title="T", body="", active="privacy.html")
    assert "<strong>Privacy</strong>" in page
    assert 'href="privacy.html"' not in page.split("</nav>")[0]


# --- The real documents ---


def test_build_produces_every_expected_file():
    files = build_all()
    assert set(files) == {"index.html", "terms.html", "privacy.html", ".nojekyll"}


def test_generated_pages_contain_no_unconverted_markdown():
    for name, content in build_all().items():
        if not name.endswith(".html"):
            continue
        body = content.split("</nav>", 1)[-1].split("<footer>", 1)[0]
        assert not re.search(r"^\s*#{1,6}\s", body, re.MULTILINE), name
        assert not re.search(r"\*\*[^*\n]+\*\*", body), name
        assert not re.search(r"\[[^\]]+\]\([^)]+\)", body), name
        assert not re.search(r"^\s*\|.*\|\s*$", body, re.MULTILINE), name


CONTACT_TEXT = "jonathan [at] wikisubmission [dot] org"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def test_each_document_keeps_a_contact_route():
    """A policy with no contact route fails its own deletion-request promise."""
    files = build_all()
    for page in PAGES:
        assert CONTACT_TEXT in files[page.output]


def test_published_pages_expose_no_harvestable_address():
    """Obfuscated on purpose: crawlers should find nothing to scrape."""
    for name, content in build_all().items():
        if not name.endswith(".html"):
            continue
        assert "mailto:" not in content, name
        assert not EMAIL_RE.search(content), name


def test_privacy_page_covers_the_archive_command():
    assert "/archive" in build_all()["privacy.html"]


def test_site_directory_is_up_to_date():
    """CI guard: regenerate with python scripts/build_legal_site.py."""
    site = ROOT / "site"
    for name, expected in build_all().items():
        target = site / name
        assert target.exists(), f"site/{name} is missing"
        assert target.read_text(encoding="utf-8") == expected, (
            f"site/{name} is stale; run python scripts/build_legal_site.py"
        )


# --- Repository hygiene ---


def _gitignore() -> str:
    return (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_gitignore_denies_the_state_folders_by_default():
    """An extension list would miss a .sqlite or .csv dropped in later."""
    text = _gitignore()
    assert "databases/*" in text
    assert "archives/*" in text
    assert "backups/" in text


def test_gitignore_allows_back_only_the_safe_files():
    text = _gitignore()
    assert "!databases/.gitkeep" in text
    assert "!databases/debate_image_map.json" in text


def test_no_local_backups_folder_is_committed():
    assert not (ROOT / "backups").exists(), (
        "backups/ holds superseded member data and should not exist"
    )


def test_voice_dependencies_are_not_declared():
    """The voice cog was removed; PyNaCl and the [voice] extra are dead weight."""
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "[voice]" not in requirements
    assert "PyNaCl" not in requirements
