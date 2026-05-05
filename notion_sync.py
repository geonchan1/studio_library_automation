#!/usr/bin/env python3
"""
notion_sync.py — Push a use-case set to Notion. Steps 5–6 of the workflow.

Per use case, the "set" we push is:
  - Library card (title, description, category, capability_tags)
  - Use-case profile (industry, doc types, pain features)
  - Workflow design (classify classes, extract schema, agent config)
  - Run-log summary (final Job ID, success rate)
  - Links to: synthetic data folder, Studio agent URL

Each use case becomes one row in a Notion database. Re-running the script
upserts (matches by `slug`) so the Library publisher can pull a stable
snapshot.

The Library publisher then runs this script on a schedule (cron / GitHub
Actions) and pushes any new rows into the Studio public Library.

  python notion_sync.py \
      --set ../trade_invoices_set/ \
      --db $NOTION_LIBRARY_DB_ID

The directory passed to --set must contain:
  use_case_profile.json   library_card.json   workflow_design.md
  end_to_end_run_log.md   (optional) synth_summary.json

Required env: NOTION_TOKEN (integration token), NOTION_LIBRARY_DB_ID
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


# ---------- Notion DB schema this script expects ----------
#
# Set this DB up once in Notion with these properties (case-sensitive):
#
#   slug          | Title       | unique key per use case
#   title         | Rich text   | Library card title
#   category      | Select      | Insurance | Logistics | Finance | …
#   capabilities  | Multi-select| Parse, Classify, Extract, Instruct
#   description   | Rich text   | the validated description string
#   industry      | Rich text
#   doc_types     | Multi-select
#   pain_features | Multi-select
#   classifier    | Rich text   | classes JSON
#   extract_schema| Rich text   | extract schema JSON
#   agent_url     | URL         | studio.upstage.ai/agents/{id}
#   sample_pdf    | Files       | optional; representative sample
#   synth_summary | Rich text   | leakage check etc.
#   run_log       | Rich text   | Job IDs, status, last_run_at
#   status        | Status      | draft / verified / published
#   last_synced   | Date

# ---------- API helpers ----------

def _headers() -> dict:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise RuntimeError(
            "NOTION_TOKEN is not set. Create an integration at "
            "https://www.notion.so/my-integrations and share the target DB.")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _query_by_slug(db_id: str, slug: str) -> dict | None:
    body = {
        "filter": {"property": "slug",
                   "title": {"equals": slug}},
        "page_size": 1,
    }
    r = requests.post(f"{NOTION_API}/databases/{db_id}/query",
                      headers=_headers(), json=body, timeout=60)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def _upsert(db_id: str, properties: dict, children: list | None = None) -> dict:
    slug = properties["slug"]["title"][0]["text"]["content"]
    existing = _query_by_slug(db_id, slug)
    if existing:
        page_id = existing["id"]
        r = requests.patch(f"{NOTION_API}/pages/{page_id}",
                           headers=_headers(),
                           json={"properties": properties}, timeout=60)
        r.raise_for_status()
        # Replace children: simplest is to leave old children and append a
        # versioned heading + new content. We'll just append.
        if children:
            requests.patch(
                f"{NOTION_API}/blocks/{page_id}/children",
                headers=_headers(),
                json={"children": children}, timeout=60,
            ).raise_for_status()
        return {"id": page_id, "action": "updated"}
    body = {
        "parent": {"database_id": db_id},
        "properties": properties,
    }
    if children:
        body["children"] = children
    r = requests.post(f"{NOTION_API}/pages",
                      headers=_headers(), json=body, timeout=60)
    r.raise_for_status()
    return {"id": r.json()["id"], "action": "created"}


# ---------- Notion property builders ----------

def _title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def _rich_text(text: str | None) -> dict:
    if not text:
        return {"rich_text": []}
    # Notion has a 2000-char limit per rich_text block
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    return {"rich_text": [{"type": "text",
                            "text": {"content": c}} for c in chunks]}


def _select(name: str | None) -> dict:
    return {"select": ({"name": name} if name else None)}


def _multi(names: list[str] | None) -> dict:
    return {"multi_select":
            [{"name": n} for n in (names or []) if n]}


def _url(u: str | None) -> dict:
    return {"url": (u or None)}


def _date(iso: str | None) -> dict:
    if not iso:
        return {"date": None}
    return {"date": {"start": iso}}


def _status(name: str | None) -> dict:
    return {"status": ({"name": name} if name else None)}


def _heading(text: str, level: int = 2) -> dict:
    key = f"heading_{level}"
    return {"object": "block", "type": key,
            key: {"rich_text": [{"type": "text",
                                  "text": {"content": text}}]}}


def _code(content: str, language: str = "json") -> dict:
    body = content[:2000]  # Notion limit per block
    return {"object": "block", "type": "code",
            "code": {"language": language,
                     "rich_text": [{"type": "text",
                                     "text": {"content": body}}]}}


def _para(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [
                {"type": "text", "text": {"content": text[:2000]}}
            ]}}


# ---------- Set loader ----------

def _load_set(set_dir: Path) -> dict:
    """Read all known artifacts from a use-case set folder."""
    def _r(path: Path) -> Any:
        if not path.exists():
            return None
        if path.suffix == ".json":
            return json.loads(path.read_text())
        return path.read_text(encoding="utf-8")

    s = {
        "use_case_profile": _r(set_dir / "use_case_profile.json"),
        "library_card":     _r(set_dir / "library_card.json"),
        "workflow_design":  _r(set_dir / "workflow_design.md"),
        "run_log":          _r(set_dir / "end_to_end_run_log.md"),
        "synth_summary":    _r(set_dir / "synth_summary.json"),
    }

    # Allow alternate filenames (e.g., the Daiso run uses _daiso suffix)
    for alt_suffix in ("_daiso", "_v2"):
        if not s["use_case_profile"]:
            s["use_case_profile"] = _r(
                set_dir / f"use_case_profile{alt_suffix}.json")
        if not s["library_card"]:
            s["library_card"] = _r(
                set_dir / f"library_card{alt_suffix}.json")
        if not s["run_log"]:
            s["run_log"] = _r(
                set_dir / f"end_to_end_run_log{alt_suffix}.md")
    return s


def _slug_for(card: dict, profile: dict) -> str:
    """Stable slug derived from category + title."""
    cat = (card.get("category") or "Others").lower()
    title = (card.get("title") or "untitled").lower().replace(" ", "-")
    return f"{cat}--{title}"


# ---------- Property packing ----------

def _properties(card: dict, profile: dict, agent_url: str | None,
                run_log: str | None,
                synth_summary_text: str | None) -> dict:
    slug = _slug_for(card, profile)
    return {
        "slug":          _title(slug),
        "title":         _rich_text(card.get("title")),
        "category":      _select(card.get("category")),
        "capabilities":  _multi(card.get("capability_tags")),
        "description":   _rich_text(card.get("description")),
        "industry":      _rich_text(profile.get("industry")),
        "doc_types":     _multi(_split_doc_types(profile)),
        "pain_features": _multi(profile.get("pain_features")),
        "classifier":    _rich_text(json.dumps(
            profile.get("classifier_classes", []),
            ensure_ascii=False)),
        "extract_schema": _rich_text(json.dumps(
            profile.get("extract_schema_draft", {}),
            ensure_ascii=False)),
        "agent_url":     _url(agent_url),
        "synth_summary": _rich_text(synth_summary_text or ""),
        "run_log":       _rich_text(run_log or ""),
        "status":        _status("draft"),
        "last_synced":   _date(_now_iso()),
    }


def _split_doc_types(profile: dict) -> list[str]:
    long = profile.get("doc_type_long") or ""
    short = profile.get("doc_type_short") or ""
    return [t for t in [short, long] if t]


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------- Body content (page children) ----------

def _children(profile: dict, card: dict, workflow_md: str | None,
              run_log: str | None) -> list[dict]:
    blocks: list[dict] = []
    blocks.append(_heading("Library card", 2))
    blocks.append(_para(card.get("description") or ""))
    if card.get("_validation"):
        blocks.append(_heading("Validation", 3))
        blocks.append(_code(json.dumps(card["_validation"], indent=2,
                                       ensure_ascii=False)))
    blocks.append(_heading("Use-case profile", 2))
    blocks.append(_code(json.dumps(profile, indent=2, ensure_ascii=False)))
    if workflow_md:
        blocks.append(_heading("Workflow design", 2))
        # split markdown into chunks of <=2000 chars
        for i in range(0, len(workflow_md), 1900):
            blocks.append(_para(workflow_md[i:i + 1900]))
    if run_log:
        blocks.append(_heading("End-to-end run log", 2))
        for i in range(0, len(run_log), 1900):
            blocks.append(_para(run_log[i:i + 1900]))
    return blocks


# ---------- Main ----------

def push_set(set_dir: Path, db_id: str, agent_url: str | None) -> dict:
    s = _load_set(set_dir)
    profile = s.get("use_case_profile") or {}
    card    = s.get("library_card") or {}
    if not card:
        raise RuntimeError(
            f"No library_card.json found in {set_dir}. "
            "Run pipeline + generate the card first.")

    synth_text = (json.dumps(s["synth_summary"], ensure_ascii=False)
                  if s.get("synth_summary") else "")
    properties = _properties(card, profile, agent_url,
                             run_log=s.get("run_log"),
                             synth_summary_text=synth_text)
    children = _children(profile, card, s.get("workflow_design"),
                         s.get("run_log"))
    return _upsert(db_id, properties, children)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=("Upsert a use-case set into a Notion database. "
                     "Idempotent on the slug field. Run on a schedule for "
                     "automatic Library refresh."))
    p.add_argument("--set", required=True,
                   help="Directory containing the use-case set artifacts.")
    p.add_argument("--db", default=os.environ.get("NOTION_LIBRARY_DB_ID"),
                   help="Notion DB id. Default: $NOTION_LIBRARY_DB_ID.")
    p.add_argument("--agent-url",
                   help="Studio agent URL to attach to the row.")
    args = p.parse_args(argv)

    if not args.db:
        print("Notion DB id is required (--db or $NOTION_LIBRARY_DB_ID).",
              file=sys.stderr)
        return 2

    res = push_set(Path(args.set), args.db, args.agent_url)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
