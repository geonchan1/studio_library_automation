#!/usr/bin/env python3
"""
seo_meta.py — Library card SEO + meta-tag generator.

Step 7 of the workflow (사용자 정리). Given a validated library_card.json,
emit:
  - <meta> tags suitable for the Studio Library card detail page
  - Open Graph + Twitter Card tags for social previews
  - JSON-LD `Product` / `SoftwareApplication` structured data
  - SEO checklist with pass/warn/fail per rule

  python seo_meta.py --card library_card.json
  python seo_meta.py --card library_card.json --html        # ready-to-paste
  python seo_meta.py --card library_card.json --check       # validation only
  python seo_meta.py --card library_card.json --slug-prefix /library

Output is written to seo_<slug>.html and seo_<slug>.json next to the card.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# ---------- Constraints (sourced from common SEO best practices) ----------
TITLE_MIN = 30
TITLE_MAX = 60
DESC_MIN = 70
DESC_MAX = 160
SLUG_REGEX = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")

LIBRARY_BASE_URL = "https://studio.upstage.ai/library"
SITE_NAME = "Upstage Studio"
DEFAULT_LOCALE = "en_US"

# OG image: standard 1200×630. Use a stable image hosted by Upstage; this
# is a placeholder URL the Library team can swap.
DEFAULT_OG_IMAGE = (
    "https://www.upstage.ai/assets/images/meta/og/library-card.png"
)


# ---------- Generators ----------

def _slugify(title: str) -> str:
    s = title.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "untitled"


def _seo_title(card_title: str, category: str) -> str:
    """Generate a page <title> in the 30-60 char sweet spot.
    Pattern: '<Card title> agent — <Category> document AI | Upstage Studio'
    """
    base = f"{card_title} agent — {category} document AI"
    suffix = " | Upstage Studio"
    if len(base) + len(suffix) <= TITLE_MAX:
        return base + suffix
    if len(base) <= TITLE_MAX:
        return base
    return base[:TITLE_MAX - 1] + "…"


def _seo_description(card_desc: str) -> str:
    """The Library card description is 50-75 words (~300-450 chars).
    Compress to ≤160 chars by taking the first sentence and trimming."""
    desc = card_desc.replace("\n", " ").strip()
    # First sentence
    first = re.split(r"(?<=[.!?])\s+", desc, maxsplit=1)[0]
    if len(first) > DESC_MAX:
        return first[:DESC_MAX - 1] + "…"
    if len(first) < DESC_MIN and len(desc) >= DESC_MIN:
        # Pad with the next sentence
        return desc[:DESC_MAX - 1] + ("…" if len(desc) > DESC_MAX else "")
    return first


def _keywords(card: dict, profile: dict | None) -> list[str]:
    """Build a small keyword list. We avoid spammy stuffing."""
    out = [card["title"].lower(), card["category"].lower(),
           "document AI", "OCR", "information extraction",
           "Upstage Studio"]
    if profile:
        out += [(profile.get("doc_type_short") or "").lower()]
        out += [t.lower() for t in (profile.get("pain_features") or [])][:3]
    # de-dupe preserving order
    seen, dedup = set(), []
    for k in out:
        if k and k not in seen:
            seen.add(k)
            dedup.append(k)
    return dedup[:10]


def build_meta(card: dict, profile: dict | None,
               slug_prefix: str = "/library",
               og_image: str = DEFAULT_OG_IMAGE) -> dict:
    title = card["title"]
    category = card.get("category", "Others")
    description_full = card["description"]

    seo_title = _seo_title(title, category)
    seo_desc = _seo_description(description_full)
    slug = _slugify(title)
    canonical = f"{LIBRARY_BASE_URL.rstrip('/')}" \
                f"{slug_prefix.rstrip('/')}/{slug}"

    keywords = _keywords(card, profile)

    json_ld = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": f"{title} agent",
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Web",
        "description": seo_desc,
        "url": canonical,
        "image": og_image,
        "creator": {
            "@type": "Organization",
            "name": "Upstage AI",
            "url": "https://www.upstage.ai",
        },
        "category": category,
        "keywords": ", ".join(keywords),
        "offers": {
            "@type": "Offer",
            "url": canonical,
            "availability": "https://schema.org/InStock",
        },
    }

    meta = {
        "title": seo_title,
        "description": seo_desc,
        "canonical": canonical,
        "keywords": keywords,
        "og": {
            "og:title": seo_title,
            "og:description": seo_desc,
            "og:url": canonical,
            "og:type": "website",
            "og:site_name": SITE_NAME,
            "og:locale": DEFAULT_LOCALE,
            "og:image": og_image,
            "og:image:width": "1200",
            "og:image:height": "630",
        },
        "twitter": {
            "twitter:card": "summary_large_image",
            "twitter:title": seo_title,
            "twitter:description": seo_desc,
            "twitter:image": og_image,
            "twitter:site": "@UpstageAI",
        },
        "json_ld": json_ld,
    }
    return meta


# ---------- HTML rendering ----------

def render_html(meta: dict) -> str:
    lines = [
        f'<title>{_esc(meta["title"])}</title>',
        f'<meta name="description" content="{_esc(meta["description"])}">',
        f'<link rel="canonical" href="{_esc(meta["canonical"])}">',
        f'<meta name="keywords" content="{_esc(", ".join(meta["keywords"]))}">',
    ]
    for k, v in meta["og"].items():
        lines.append(f'<meta property="{k}" content="{_esc(v)}">')
    for k, v in meta["twitter"].items():
        lines.append(f'<meta name="{k}" content="{_esc(v)}">')
    lines.append('<script type="application/ld+json">')
    lines.append(json.dumps(meta["json_ld"], indent=2, ensure_ascii=False))
    lines.append('</script>')
    return "\n".join(lines)


def _esc(s: Any) -> str:
    if not isinstance(s, str):
        s = str(s)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


# ---------- Validation ----------

def validate(meta: dict, card: dict) -> dict:
    """Return a list of {id, severity, msg, fix} dicts."""
    issues = []
    title_len = len(meta["title"])
    desc_len = len(meta["description"])

    def add(id_, sev, msg, fix=None):
        issues.append({"id": id_, "severity": sev, "msg": msg, "fix": fix})

    # Title length
    if title_len < TITLE_MIN:
        add("S1", "warn",
            f"<title> is {title_len} chars; aim for {TITLE_MIN}-{TITLE_MAX}.",
            "Add the category or 'Document AI' suffix.")
    elif title_len > TITLE_MAX:
        add("S1", "fail",
            f"<title> is {title_len} chars; max {TITLE_MAX} or Google "
            "truncates.", "Drop the suffix or shorten the agent title.")

    # Description length
    if desc_len < DESC_MIN:
        add("S2", "warn",
            f"meta description is {desc_len} chars; aim for {DESC_MIN}-"
            f"{DESC_MAX}.",
            "Pad with the second sentence of the card description.")
    elif desc_len > DESC_MAX:
        add("S2", "fail",
            f"meta description is {desc_len} chars; max {DESC_MAX} or "
            "Google truncates.", "Trim to the first sentence.")

    # Slug
    slug = meta["canonical"].rstrip("/").rsplit("/", 1)[-1]
    if not SLUG_REGEX.match(slug):
        add("S3", "fail",
            f"Canonical slug '{slug}' contains disallowed characters.",
            "Lowercase, hyphens only, 2-60 chars.")

    # OG image — must be absolute https URL
    og_img = meta["og"]["og:image"]
    if not og_img.startswith("https://"):
        add("S4", "fail",
            "og:image must be an absolute https:// URL.",
            "Host the image on a CDN over HTTPS.")

    # Card-level sanity (mirror the library-card-writer rules)
    if not (50 <= len(card.get("description", "").split()) <= 80):
        add("C1", "warn",
            "Card description should be 50-75 words for the Library card "
            "rendering, currently outside that band.",
            "Re-validate via library-card-writer before publishing.")

    if " AI-powered " in card.get("description", "") \
       or " revolutionary " in card.get("description", "").lower():
        add("C2", "fail",
            "Forbidden marketing token in description (per "
            "library-card-writer rules).", "Rewrite without buzzwords.")

    return {"issues": issues,
            "passed": all(i["severity"] != "fail" for i in issues)}


# ---------- Main ----------

def _load_card(path: Path) -> dict:
    return json.loads(path.read_text())


def _maybe_load_profile(card_path: Path) -> dict | None:
    cand = card_path.parent / "use_case_profile.json"
    if cand.exists():
        return json.loads(cand.read_text())
    # Daiso/v2 alt suffix
    for s in ("_daiso", "_v2"):
        cand2 = card_path.parent / f"use_case_profile{s}.json"
        if cand2.exists():
            return json.loads(cand2.read_text())
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Generate SEO + Open Graph + JSON-LD meta tags for a "
                    "Library card.")
    p.add_argument("--card", required=True,
                   help="Path to library_card.json")
    p.add_argument("--slug-prefix", default="/library",
                   help="URL path prefix under studio.upstage.ai. "
                        "Default: /library")
    p.add_argument("--og-image", default=DEFAULT_OG_IMAGE)
    p.add_argument("--html", action="store_true",
                   help="Print the rendered HTML <head> snippet to stdout.")
    p.add_argument("--check", action="store_true",
                   help="Print validation issues and exit non-zero on fail.")
    p.add_argument("--out-dir",
                   help="Directory to write seo_<slug>.html and "
                        "seo_<slug>.json. Default: card's directory.")
    args = p.parse_args(argv)

    card_path = Path(args.card)
    card = _load_card(card_path)
    profile = _maybe_load_profile(card_path)
    out_dir = Path(args.out_dir) if args.out_dir else card_path.parent

    meta = build_meta(card, profile,
                      slug_prefix=args.slug_prefix,
                      og_image=args.og_image)
    html = render_html(meta)
    issues = validate(meta, card)

    slug = meta["canonical"].rstrip("/").rsplit("/", 1)[-1]
    json_path = out_dir / f"seo_{slug}.json"
    html_path = out_dir / f"seo_{slug}.html"
    json_path.write_text(json.dumps(
        {"meta": meta, "validation": issues},
        ensure_ascii=False, indent=2))
    html_path.write_text(html)

    if args.html:
        print(html)
    elif args.check:
        print(json.dumps(issues, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({
            "title": meta["title"],
            "description": meta["description"],
            "canonical": meta["canonical"],
            "validation_passed": issues["passed"],
            "files": {"html": str(html_path), "json": str(json_path)},
        }, ensure_ascii=False, indent=2))

    return 0 if issues["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
