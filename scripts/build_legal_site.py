"""Generate the public legal site in site/ from the markdown in docs/.

Discord's validator fetches the Terms of Service and Privacy Policy URLs and
rejects anything it cannot reach, so both documents need to be served as real
public web pages. The markdown in docs/ stays the single source of truth and
this script renders it, so the two cannot drift apart. CI checks the output is
current.

    python scripts/build_legal_site.py          # write site/
    python scripts/build_legal_site.py --check  # fail if site/ is stale
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SITE = ROOT / "site"

APP_NAME = "19 Bot"

# Written out rather than as a mailto: link, so address-harvesting crawlers
# cannot lift it off the published pages.
CONTACT = "jonathan [at] wikisubmission [dot] org"


@dataclass(frozen=True)
class Page:
    source: Path
    output: str
    title: str
    nav_label: str


PAGES = (
    Page(DOCS / "TERMS_OF_SERVICE.md", "terms.html", "Terms of Service", "Terms"),
    Page(DOCS / "PRIVACY_POLICY.md", "privacy.html", "Privacy Policy", "Privacy"),
)

_STYLE = """\
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #1c1e21;
  --muted: #5b6168;
  --rule: #e3e6ea;
  --accent: #3d5afe;
  --code-bg: #f4f6f8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181c;
    --fg: #e6e8ea;
    --muted: #9aa2ab;
    --rule: #2c3036;
    --accent: #8fa2ff;
    --code-bg: #1f2228;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
  -webkit-text-size-adjust: 100%;
}
.wrap { max-width: 46rem; margin: 0 auto; padding: 2.5rem 1.25rem 5rem; }
nav {
  border-bottom: 1px solid var(--rule);
  margin-bottom: 2.5rem;
  padding-bottom: 1rem;
  display: flex;
  flex-wrap: wrap;
  gap: 1.25rem;
  align-items: baseline;
}
nav .brand { font-weight: 700; letter-spacing: -0.01em; }
nav a { color: var(--muted); text-decoration: none; }
nav a:hover, nav a:focus { color: var(--accent); text-decoration: underline; }
h1 { font-size: 1.9rem; line-height: 1.25; letter-spacing: -0.02em; margin: 0 0 1.25rem; }
h2 { font-size: 1.3rem; margin: 2.5rem 0 0.75rem; letter-spacing: -0.01em; }
h3 { font-size: 1.05rem; margin: 2rem 0 0.5rem; }
p, ul, ol { margin: 0 0 1rem; }
li { margin-bottom: 0.35rem; }
a { color: var(--accent); }
hr { border: 0; border-top: 1px solid var(--rule); margin: 2.5rem 0; }
code {
  background: var(--code-bg);
  padding: 0.15em 0.4em;
  border-radius: 4px;
  font-size: 0.9em;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
pre {
  background: var(--code-bg);
  padding: 0.9rem 1rem;
  border-radius: 6px;
  overflow-x: auto;
}
pre code { background: none; padding: 0; }
blockquote {
  margin: 0 0 1rem;
  padding: 0.1rem 0 0.1rem 1rem;
  border-left: 3px solid var(--rule);
  color: var(--muted);
}
.table-scroll { overflow-x: auto; margin: 0 0 1.25rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.94rem; }
th, td { text-align: left; padding: 0.5rem 0.7rem; border-bottom: 1px solid var(--rule); }
th { font-weight: 600; }
footer {
  margin-top: 3.5rem;
  padding-top: 1.25rem;
  border-top: 1px solid var(--rule);
  color: var(--muted);
  font-size: 0.9rem;
}
"""


# --- Inline markdown ---

_INLINE_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_BARE_URL = re.compile(r"<(https?://[^>\s]+)>")


def render_inline(text: str) -> str:
    """Convert inline markdown to HTML, escaping everything else."""
    placeholders: list[str] = []

    def stash(markup: str) -> str:
        placeholders.append(markup)
        return f"\x00{len(placeholders) - 1}\x00"

    # Code first: its contents must not be treated as markdown.
    text = _INLINE_CODE.sub(lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _BARE_URL.sub(
        lambda m: stash(
            f'<a href="{html.escape(m.group(1), quote=True)}">{html.escape(m.group(1))}</a>'
        ),
        text,
    )
    text = _LINK.sub(
        lambda m: stash(
            f'<a href="{html.escape(_rewrite_link(m.group(2)), quote=True)}">'
            f"{html.escape(m.group(1))}</a>"
        ),
        text,
    )

    text = html.escape(text)
    text = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", text)

    for index, markup in enumerate(placeholders):
        text = text.replace(f"\x00{index}\x00", markup)
    return text


def _rewrite_link(href: str) -> str:
    """Point cross-document markdown links at their published pages."""
    mapping = {
        "PRIVACY_POLICY.md": "privacy.html",
        "TERMS_OF_SERVICE.md": "terms.html",
        "docs/PRIVACY_POLICY.md": "privacy.html",
        "docs/TERMS_OF_SERVICE.md": "terms.html",
    }
    return mapping.get(href, href)


# --- Block markdown ---


def render_markdown(source: str) -> str:
    lines = source.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("```"):
            index += 1
            block: list[str] = []
            while index < total and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(block))}</code></pre>")
            continue

        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            out.append("<hr>")
            index += 1
            continue

        heading = re.match(r"(#{1,6})\s+(.*)", stripped)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{render_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        # Table: a header row followed by a separator row.
        if (
            stripped.startswith("|")
            and index + 1 < total
            and re.fullmatch(r"\|[\s:|-]+\|", lines[index + 1].strip())
        ):
            header = _split_row(stripped)
            index += 2
            body: list[list[str]] = []
            while index < total and lines[index].strip().startswith("|"):
                body.append(_split_row(lines[index].strip()))
                index += 1
            out.append(_render_table(header, body))
            continue

        if stripped.startswith(">"):
            block = []
            while index < total and lines[index].strip().startswith(">"):
                block.append(lines[index].strip().lstrip(">").strip())
                index += 1
            out.append(f"<blockquote><p>{render_inline(' '.join(block))}</p></blockquote>")
            continue

        bullet = re.match(r"[-*+]\s+(.*)", stripped)
        numbered = re.match(r"\d+[.)]\s+(.*)", stripped)
        if bullet or numbered:
            ordered = numbered is not None
            items, index = _collect_list(lines, index, ordered)
            tag = "ol" if ordered else "ul"
            rendered = "".join(f"<li>{render_inline(item)}</li>" for item in items)
            out.append(f"<{tag}>{rendered}</{tag}>")
            continue

        # Paragraph: consume until a blank line or the start of another block.
        block = []
        while index < total and lines[index].strip():
            candidate = lines[index].strip()
            if candidate.startswith(("#", "|", ">", "```")) or re.match(
                r"([-*+]\s+|\d+[.)]\s+)", candidate
            ):
                break
            if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", candidate):
                break
            # Two trailing spaces is a markdown hard break; keep it as <br>.
            block.append(candidate + ("  " if lines[index].endswith("  ") else ""))
            index += 1
        if block:
            joined = " ".join(block)
            parts = [render_inline(part.strip()) for part in joined.split("  ") if part.strip()]
            out.append("<p>" + "<br>\n".join(parts) + "</p>")

    return "\n".join(out)


def _collect_list(lines: list[str], index: int, ordered: bool) -> tuple[list[str], int]:
    pattern = r"\d+[.)]\s+(.*)" if ordered else r"[-*+]\s+(.*)"
    items: list[str] = []
    total = len(lines)
    while index < total:
        stripped = lines[index].strip()
        match = re.fullmatch(pattern, stripped)
        if match:
            items.append(match.group(1))
            index += 1
            continue
        # A plain indented line continues the previous item.
        if stripped and lines[index].startswith((" ", "\t")) and items:
            items[-1] += " " + stripped
            index += 1
            continue
        break
    return items, index


def _split_row(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _render_table(header: list[str], body: list[list[str]]) -> str:
    head = "".join(f"<th>{render_inline(cell)}</th>" for cell in header)
    rows = []
    for row in body:
        cells = "".join(f"<td>{render_inline(cell)}</td>" for cell in row)
        rows.append(f"<tr>{cells}</tr>")
    return (
        '<div class="table-scroll"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


# --- Page assembly ---


def _nav(active: str) -> str:
    links = [f'<span class="brand">{APP_NAME}</span>']
    entries = [("Home", "index.html")] + [(p.nav_label, p.output) for p in PAGES]
    for label, href in entries:
        if href == active:
            links.append(f"<strong>{label}</strong>")
        else:
            links.append(f'<a href="{href}">{label}</a>')
    return "<nav>" + "\n".join(links) + "</nav>"


def wrap_page(*, title: str, body: str, active: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — {APP_NAME}</title>
<meta name="description" content="{html.escape(title)} for the {APP_NAME} Discord application.">
<meta name="robots" content="index, follow">
<style>
{_STYLE}</style>
</head>
<body>
<div class="wrap">
{_nav(active)}
{body}
<footer>
<p>{APP_NAME} — a moderation and utility Discord application.
Contact: {CONTACT}</p>
</footer>
</div>
</body>
</html>
"""


def build_index() -> str:
    body = f"""<h1>{APP_NAME}</h1>
<p>{APP_NAME} is a moderation and utility application for Discord, operated for
The Submission Server. These are its policy documents.</p>
<div class="table-scroll"><table><thead><tr><th>Document</th><th>What it covers</th></tr></thead><tbody>
<tr><td><a href="terms.html">Terms of Service</a></td><td>The terms you agree to by adding or using the bot</td></tr>
<tr><td><a href="privacy.html">Privacy Policy</a></td><td>Exactly what data is stored, for how long, and how to have it deleted</td></tr>
</tbody></table></div>
<h2>Requesting your data</h2>
<p>You may request a copy of your data, or its deletion, at any time and free of
charge by emailing
{CONTACT}.
Include your Discord user ID. Requests are actioned within 30 days.</p>
"""
    return wrap_page(title="Policies", body=body, active="index.html")


def build_all() -> dict[str, str]:
    files: dict[str, str] = {"index.html": build_index()}
    for page in PAGES:
        markdown = page.source.read_text(encoding="utf-8")
        files[page.output] = wrap_page(
            title=page.title,
            body=render_markdown(markdown),
            active=page.output,
        )
    # Serve the HTML as-is rather than letting GitHub Pages run Jekyll over it.
    files[".nojekyll"] = ""
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if site/ does not match the markdown",
    )
    args = parser.parse_args()

    files = build_all()

    if args.check:
        stale = []
        for name, content in files.items():
            target = SITE / name
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                stale.append(name)
        if stale:
            print("site/ is out of date: " + ", ".join(sorted(stale)))
            print("Run: python scripts/build_legal_site.py")
            return 1
        print(f"site/ is up to date ({len(files)} files)")
        return 0

    SITE.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (SITE / name).write_text(content, encoding="utf-8", newline="\n")
        print(f"wrote site/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
