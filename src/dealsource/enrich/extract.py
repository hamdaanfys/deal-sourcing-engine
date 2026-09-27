"""HTML -> clean text with selectolax, page selection, and contact-info scrubbing (§8.2)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from dealsource.privacy import scrub_contact_info
from dealsource.resolve.normalize import domain_key

REMOVE_TAGS = (
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "nav",
    "footer",
    "header",
    "form",
    "template",
)

# Pages worth reading, in priority order (matched against the URL path and the link text).
INCLUDE_KEYWORDS = (
    "about",
    "company",
    "who-we-are",
    "history",
    "overview",
    "products",
    "product",
    "services",
    "solutions",
    "capabilities",
    "what-we-do",
    "industries",
    "markets",
    "manufacturing",
    "facilities",
    "locations",
)
# Pages never fetched: contact details, people, and account/legal pages.
SKIP_KEYWORDS = (
    "contact",
    "team",
    "leadership",
    "staff",
    "people",
    "management",
    "executive",
    "board",
    "director",
    "governance",
    "career",
    "job",
    "employment",
    "privacy",
    "terms",
    "cookie",
    "legal",
    "login",
    "log-in",
    "signin",
    "sign-in",
    "account",
    "cart",
    "checkout",
    "register",
)
SKIP_EXTENSIONS = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".zip",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".mp4",
    ".mp3",
    ".dwg",
    ".step",
    ".stp",
)


@dataclass(frozen=True)
class PageText:
    path: str
    title: str
    description: str
    text: str


def extract_text(html: str) -> tuple[str, str, str]:
    """Return (title, meta description, visible body text) with boilerplate removed."""
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = _clean(title_node.text()) if title_node else ""
    meta = tree.css_first('meta[name="description"]')
    description = _clean(meta.attributes.get("content") or "") if meta else ""
    for tag in REMOVE_TAGS:
        for node in tree.css(tag):
            node.decompose()
    body = tree.body
    raw = body.text(separator="\n") if body else ""
    lines, seen = [], set()
    for line in raw.splitlines():
        line = _clean(line)
        if len(line) < 2 or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return title, description, "\n".join(lines)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_SKIP_RE = re.compile(r"(?<![a-z])(?:" + "|".join(map(re.escape, SKIP_KEYWORDS)) + ")")


def _is_skipped(text: str) -> bool:
    # Keywords match at the start of a word: "teams" is skipped, "steam-boilers" is not.
    return bool(_SKIP_RE.search(text.lower()))


def _include_rank(text: str) -> int | None:
    t = text.lower()
    for i, k in enumerate(INCLUDE_KEYWORDS):
        if re.search(r"(?<![a-z])" + re.escape(k), t):
            return i
    return None


def select_links(html: str, base_url: str, site_domain: str, limit: int) -> list[str]:
    """Same-site pages likely to describe the business, best first; never contact/people pages."""
    tree = HTMLParser(html)
    candidates: dict[str, tuple[int, int]] = {}
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or domain_key(url) != site_domain:
            continue
        path = parts.path or "/"
        if path in ("", "/") or path.lower().endswith(SKIP_EXTENSIONS):
            continue
        label = f"{path} {_clean(a.text() or '')}"
        if _is_skipped(label):
            continue
        rank = _include_rank(label)
        if rank is None:
            continue
        clean = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        score = (rank, path.count("/"))
        if clean not in candidates or score < candidates[clean]:
            candidates[clean] = score
    ordered = sorted(candidates, key=lambda u: (candidates[u], u))
    return ordered[:limit]


def page_text(url: str, html: str) -> PageText:
    title, description, text = extract_text(html)
    path = urlsplit(url).path or "/"
    return PageText(
        path=path,
        title=scrub_contact_info(title),
        description=scrub_contact_info(description),
        text=scrub_contact_info(text),
    )


def build_document(pages: list[PageText], *, budget_chars: int, per_page_chars: int) -> str:
    """Concatenate pages under '### <path>' headers within a character budget."""
    parts, used = [], 0
    for p in pages:
        header = f"### {p.path}"
        body = "\n".join(x for x in (p.title, p.description, p.text) if x)[:per_page_chars]
        chunk = f"{header}\n{body}".strip()
        if used + len(chunk) > budget_chars:
            chunk = chunk[: max(0, budget_chars - used)]
        if chunk:
            parts.append(chunk)
            used += len(chunk) + 2
        if used >= budget_chars:
            break
    return "\n\n".join(parts)
