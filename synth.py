#!/usr/bin/env python3
"""
synth.py — Phase 2 synthetic-data generator.

  Real PDF ─► Document Parse (HTML+text) ─► Solar Pro2 (synth prompt)
              ─► Synthetic HTML ─► (optional) WeasyPrint ─► Synthetic PDF

The Solar prompt enforces layout/script preservation while swapping
identifying values. See ../synth_data.system.txt for the full rule set.

  python synth.py input.pdf
  python synth.py input.pdf --out synth.html
  python synth.py input.pdf --out synth.pdf --render
  python synth.py input.pdf --hint industry='household plastics'

Verification step (RECOMMENDED before sharing the synthetic file):
  python pipeline.py synth.pdf  # should yield similar extraction shape
                                 # but no original values present
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

API_BASE = "https://api.upstage.ai/v1"
DOCUMENT_PARSE_URL = f"{API_BASE}/document-digitization"
CHAT_URL = f"{API_BASE}/chat/completions"

PARSE_MODEL = "document-parse"
SYNTH_MODEL = os.environ.get("UPSTAGE_SYNTH_MODEL", "solar-pro2")

# Resolve prompt files relative to either the outputs root (where the
# synth_data prompt was originally saved) or the automation/ folder
# itself, so the script works whether you copy it next to the prompts
# or run it in-place from outputs/automation/.
HERE = Path(__file__).resolve().parent
PROMPT_SEARCH = [
    HERE / "prompts",
    HERE.parent,
    HERE,
]


def _api_key() -> str:
    key = os.environ.get("UPSTAGE_API_KEY")
    if not key:
        raise RuntimeError(
            "UPSTAGE_API_KEY env var is not set. Get one at "
            "https://console.upstage.ai/api-keys."
        )
    return key


def _read_prompt(name: str) -> str:
    for root in PROMPT_SEARCH:
        p = root / name
        if p.exists():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"Could not find {name} in any of {PROMPT_SEARCH}. "
        "Place synth_data.system.txt and synth_data.user.txt next to "
        "synth.py (or in ./prompts/) before running.")


def parse_to_html(pdf_path: Path) -> dict:
    """Run Document Parse and return the {html, text, ...} response."""
    headers = {"Authorization": f"Bearer {_api_key()}"}
    with open(pdf_path, "rb") as f:
        files = {"document": (pdf_path.name, f, "application/pdf")}
        data = {
            "model": PARSE_MODEL,
            "output_formats": json.dumps(["html", "text"]),
        }
        resp = requests.post(DOCUMENT_PARSE_URL, headers=headers,
                             files=files, data=data, timeout=180)
    if not resp.ok:
        raise RuntimeError(f"Parse failed: HTTP {resp.status_code} – "
                           f"{resp.text[:300]}")
    return resp.json()


def call_synth_llm(parsed_layout: dict, hints: dict | None = None,
                   model: str | None = None) -> str:
    """
    Call Solar (or any chat-completion model on the Upstage endpoint)
    with the synth_data prompt and return the model's text response.
    The model is asked to return JSON-only.
    """
    sys_prompt = _read_prompt("synth_data.system.txt")
    user_template = _read_prompt("synth_data.user.txt")

    h = hints or {}
    user_prompt = user_template.format(
        parsed_layout_json=json.dumps(parsed_layout, ensure_ascii=False),
        target_industry=h.get("industry", ""),
        country_pair=h.get("country_pair", ""),
        target_currency=h.get("currency", ""),
        extra_fields=json.dumps(h.get("preserve_extra", [])),
        extra_swap_fields=json.dumps(h.get("swap_extra", [])),
    )

    body = {
        "model": model or SYNTH_MODEL,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.4,
    }
    resp = requests.post(
        CHAT_URL,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=240,
    )
    if not resp.ok:
        raise RuntimeError(f"Solar synth failed: HTTP {resp.status_code} – "
                           f"{resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def html_from_synth_response(synth_json_text: str) -> str:
    """
    The synth prompt may return either:
      a) {"html": "...", "text": "..."}     # mirroring Document Parse
      b) the bare HTML string in 'html' field
      c) some other shape — fall back to dumping the JSON
    Return a complete HTML document (wrapped if needed).
    """
    try:
        synth = json.loads(synth_json_text)
    except json.JSONDecodeError:
        # Already raw HTML, wrap it
        return _wrap_html_doc(synth_json_text)
    if isinstance(synth, dict) and "html" in synth:
        return _wrap_html_doc(synth["html"])
    return _wrap_html_doc(
        "<pre>" + json.dumps(synth, ensure_ascii=False, indent=2)
        + "</pre>"
    )


def _wrap_html_doc(body_html: str) -> str:
    """Wrap body fragment in a self-contained HTML page that prints sanely.
    Avoid system-fonts only — provide CJK fallback so hanzi renders.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Synthetic document</title>
<style>
  @page {{ size: A4; margin: 18mm; }}
  body {{
    font-family: "Helvetica", "Arial", "PingFang SC", "Noto Sans CJK SC",
                 "Microsoft YaHei", sans-serif;
    font-size: 11pt; color: #111;
  }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 0.5px solid #888; padding: 4px 6px; }}
  th {{ background: #eee; }}
  h1, h2 {{ font-weight: 700; }}
</style>
</head>
<body>
{body_html}
</body>
</html>
"""


def render_html_to_pdf(html: str, out_pdf: Path) -> None:
    """Render HTML to PDF. Tries WeasyPrint first; falls back to a clear
    error message if it isn't installed (system deps are heavy on macOS).
    """
    try:
        from weasyprint import HTML  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "PDF rendering requires WeasyPrint. Install with:\n"
            "  pip install weasyprint --break-system-packages\n"
            "  brew install pango cairo glib gdk-pixbuf libffi  # macOS\n"
            f"(import error: {e})"
        )
    HTML(string=html).write_pdf(str(out_pdf))


# ---------- end-to-end ----------

def synthesize(pdf_path: Path, hints: dict | None = None,
               include_parse_in_output: bool = False) -> dict:
    parsed = parse_to_html(pdf_path)

    # Strip down what we send to Solar — html + text is plenty
    payload = {k: parsed[k] for k in ("html", "text") if k in parsed}
    if not payload:
        # Some responses use 'content' key; fall back generically
        payload = parsed

    synth_text = call_synth_llm(payload, hints=hints)
    synth_html = html_from_synth_response(synth_text)

    out = {
        "source_file": str(pdf_path),
        "model": SYNTH_MODEL,
        "synth_html": synth_html,
        "synth_raw": synth_text,
    }
    if include_parse_in_output:
        out["parse"] = parsed
    return out


def _parse_hints(args_list: list[str]) -> dict:
    """--hint key=value --hint key=value → dict."""
    h: dict[str, Any] = {}
    for s in args_list or []:
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        h[k.strip()] = v.strip()
    return h


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Generate a layout-preserving synthetic copy of a "
                    "customer document via Document Parse + Solar.")
    p.add_argument("pdf", help="Path to the real customer PDF.")
    p.add_argument("--out", help="Output path. Default: <pdf>.synth.html. "
                                 "Use a .pdf suffix with --render to also "
                                 "rasterize.")
    p.add_argument("--render", action="store_true",
                   help="If --out ends in .pdf, also render via WeasyPrint.")
    p.add_argument("--include-parse", action="store_true",
                   help="Save the original Document Parse response next to "
                        "the synth output for debugging.")
    p.add_argument("--hint", action="append", default=[],
                   help="Add a hint as key=value. Examples: "
                        "industry='household plastics', "
                        "country_pair='CN -> KR', currency=USD. "
                        "Repeatable.")
    p.add_argument("--model", help="Override the chat model. "
                                   "Default: $UPSTAGE_SYNTH_MODEL or "
                                   "solar-pro2.")
    args = p.parse_args(argv)

    if args.model:
        global SYNTH_MODEL
        SYNTH_MODEL = args.model

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}", file=sys.stderr)
        return 2

    hints = _parse_hints(args.hint)
    out_path = Path(args.out) if args.out \
        else pdf_path.with_suffix(".synth.html")

    print(f"Parsing {pdf_path.name}…", file=sys.stderr)
    print(f"Calling synth model {SYNTH_MODEL}…", file=sys.stderr)
    result = synthesize(pdf_path, hints=hints,
                        include_parse_in_output=args.include_parse)

    if out_path.suffix.lower() == ".pdf" and args.render:
        render_html_to_pdf(result["synth_html"], out_path)
        # Also drop the HTML next to it for inspection
        html_path = out_path.with_suffix(".html")
        html_path.write_text(result["synth_html"], encoding="utf-8")
        print(f"Wrote synthetic PDF → {out_path}", file=sys.stderr)
        print(f"Wrote synthetic HTML → {html_path}", file=sys.stderr)
    else:
        out_path.write_text(result["synth_html"], encoding="utf-8")
        print(f"Wrote synthetic HTML → {out_path}", file=sys.stderr)
        if args.render:
            print("(Skipped PDF render: --out does not end in .pdf)",
                  file=sys.stderr)

    if args.include_parse:
        debug_path = out_path.with_suffix(".parse.json")
        debug_path.write_text(
            json.dumps(result.get("parse", {}), ensure_ascii=False, indent=2)
        )
        print(f"Saved original parse → {debug_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
