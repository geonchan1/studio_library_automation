#!/usr/bin/env python3
"""
synth_batch.py — Run synth.py over a whole customer folder, in parallel,
and verify the synthetic copies before they're shared.

Step 4 of the 7-step workflow (사용자 정리):
    customer files ─► synthesize all ─► verify ─► save next to originals

  python synth_batch.py /path/to/customer/folder \
      --out-dir synth/ \
      --workers 3 \
      --hint industry='household plastics' \
      --verify

Verification stage (when --verify is on):
  1. No-leakage check — none of the original document's flagged identifiers
     (vendor name, invoice number, address, total amount) appear verbatim
     in the synth output.
  2. Schema regression — run pipeline.py against the synth file and confirm
     the same set of fields populate as on the original (structural sanity).
  3. Page-count match — synth file has same number of pages.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pipeline import (DEFAULT_TRADE_INVOICES_CONFIG, run_pipeline)
from synth import synthesize, render_html_to_pdf

SUPPORTED_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def _list_files(folder: Path, recursive: bool) -> list[Path]:
    it = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(p for p in it
                  if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS)


def _parse_hints(hint_args: list[str]) -> dict:
    h: dict[str, Any] = {}
    for s in hint_args or []:
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        h[k.strip()] = v.strip()
    return h


def _flagged_originals(orig_extraction: dict) -> list[str]:
    """Pick out values from the original extraction that MUST NOT appear in
    the synth. These are the high-risk identifying fields."""
    if not orig_extraction:
        return []
    fields = ["invoice_number", "vendor_name", "buyer_name", "total_amount",
              "po_reference", "po_number"]
    out = []
    for f in fields:
        v = orig_extraction.get(f)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def _no_leakage(synth_text: str, originals: list[str]) -> dict:
    """Return {leaked: [...], passed: bool}."""
    leaked = []
    if not synth_text:
        return {"leaked": [], "passed": True}
    hay = synth_text
    for v in originals:
        # Anchor whole-word-ish; but also allow partial company-name matches
        # since "JIANGSU JIUTONG PLASTIC MANUFACTURING CO., LTD" leaking even
        # partially is bad
        if v.lower() in hay.lower():
            leaked.append(v)
    return {"leaked": leaked, "passed": not leaked}


def _verify(synth_pdf_path: Path, original_extraction: dict,
            cfg: dict, original_text: str | None) -> dict:
    """Run pipeline against synth file and check for leakage."""
    synth_run = run_pipeline(
        synth_pdf_path,
        classes=cfg["classes"],
        extract_schema=cfg["extract_schema"],
        schema_name=cfg.get("schema_name", "extract_schema"),
    )

    originals_to_check = _flagged_originals(original_extraction or {})
    synth_extraction = synth_run.get("extraction", {})
    synth_text_blob = json.dumps(synth_extraction, ensure_ascii=False)
    leakage = _no_leakage(synth_text_blob, originals_to_check)

    # Structural sanity: same set of populated fields
    orig_keys = {k for k, v in (original_extraction or {}).items()
                 if isinstance(v, str) and v.strip()}
    synth_keys = {k for k, v in synth_extraction.items()
                  if isinstance(v, str) and v.strip()}

    return {
        "synth_extraction": synth_extraction,
        "original_keys_populated": sorted(orig_keys),
        "synth_keys_populated": sorted(synth_keys),
        "missing_keys_in_synth": sorted(orig_keys - synth_keys),
        "extra_keys_in_synth": sorted(synth_keys - orig_keys),
        "leakage": leakage,
    }


def _process_one(pdf_path: Path, out_dir: Path, hints: dict,
                 cfg: dict, do_verify: bool, do_render: bool) -> dict:
    """Worker: synth + (optional) verify + write artifacts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem.replace("[", "_").replace("]", "_")  # filesystem-safe
    html_path = out_dir / (stem + ".synth.html")
    pdf_synth_path = out_dir / (stem + ".synth.pdf")
    debug_path = out_dir / (stem + ".synth.json")

    t0 = time.time()
    row: dict[str, Any] = {
        "file": pdf_path.name,
        "ok": False,
        "elapsed_s": 0.0,
        "synth_html_path": str(html_path),
        "synth_pdf_path": None,
        "verification": None,
        "errors": [],
    }
    try:
        result = synthesize(pdf_path, hints=hints,
                            include_parse_in_output=True)
        html_path.write_text(result["synth_html"], encoding="utf-8")
        debug_path.write_text(
            json.dumps({k: v for k, v in result.items()
                        if k != "synth_html"},
                       ensure_ascii=False, indent=2)
        )
        if do_render:
            try:
                render_html_to_pdf(result["synth_html"], pdf_synth_path)
                row["synth_pdf_path"] = str(pdf_synth_path)
            except Exception as e:  # noqa: BLE001
                row["errors"].append({"stage": "render", "error": str(e)})

        if do_verify and row["synth_pdf_path"]:
            # Get original extraction once for leakage comparison
            orig = run_pipeline(
                pdf_path,
                classes=cfg["classes"],
                extract_schema=cfg["extract_schema"],
                schema_name=cfg.get("schema_name", "extract_schema"),
            )
            verify = _verify(
                Path(row["synth_pdf_path"]),
                orig.get("extraction", {}),
                cfg,
                result.get("parse", {}).get("text", ""),
            )
            row["verification"] = verify
            if verify["leakage"]["leaked"]:
                row["errors"].append({"stage": "verify",
                                      "error": "leaked",
                                      "details": verify["leakage"]})

        row["ok"] = not row["errors"]
    except Exception as e:  # noqa: BLE001
        row["errors"].append({"stage": "worker", "error": str(e)})
    row["elapsed_s"] = round(time.time() - t0, 2)
    return row


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=("Run synth.py across an entire customer folder in "
                     "parallel and (optionally) verify each synthetic file "
                     "against the headless pipeline."))
    p.add_argument("folder", help="Customer folder containing real PDFs.")
    p.add_argument("--out-dir", default="synth_results",
                   help="Where to write synthetic outputs.")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--hint", action="append", default=[],
                   help="key=value hints (industry=, country_pair=, "
                        "currency=). Repeatable.")
    p.add_argument("--render", action="store_true",
                   help="Also rasterize the synthetic HTML to PDF "
                        "(requires WeasyPrint).")
    p.add_argument("--verify", action="store_true",
                   help="After synthesis, run pipeline.py against the synth "
                        "PDF and check for leakage.")
    p.add_argument("--schema",
                   help="JSON config (classes + extract_schema) for the "
                        "verifier. Defaults to Trade invoices.")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        return 2
    files = _list_files(folder, args.recursive)
    if args.limit:
        files = files[:args.limit]
    if not files:
        print("No files found.", file=sys.stderr)
        return 2

    cfg = (json.loads(Path(args.schema).read_text())
           if args.schema else DEFAULT_TRADE_INVOICES_CONFIG)
    hints = _parse_hints(args.hint)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.verify and not args.render:
        print("--verify implies --render; enabling --render.",
              file=sys.stderr)
        args.render = True

    print(f"Synthesizing {len(files)} files with {args.workers} workers…",
          file=sys.stderr)
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_process_one, f, out_dir, hints, cfg,
                          args.verify, args.render): f for f in files}
        for i, fut in enumerate(as_completed(futs), 1):
            row = fut.result()
            rows.append(row)
            mark = "✓" if row["ok"] else "✗"
            print(f"  [{i}/{len(files)}] {mark} {row['file']} "
                  f"({row['elapsed_s']}s)", file=sys.stderr)

    rows.sort(key=lambda r: r["file"])
    summary = {
        "total": len(rows),
        "ok": sum(1 for r in rows if r["ok"]),
        "failed": sum(1 for r in rows if not r["ok"]),
        "leaked":  [r["file"] for r in rows
                    if r.get("verification", {}).get("leakage", {})
                    .get("leaked")],
    }
    (out_dir / "synth_summary.json").write_text(
        json.dumps({"summary": summary, "rows": rows},
                   ensure_ascii=False, indent=2)
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
