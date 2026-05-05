#!/usr/bin/env python3
"""
batch.py — Run pipeline.py over a whole folder of PDFs in parallel.

  python batch.py /path/to/folder \
      --schema schemas/trade_invoices.json \
      --out-dir results/ \
      --workers 4 \
      --csv results/summary.csv

Produces, per input file:
  - results/<stem>.result.json   (full pipeline output)
And one summary at the end:
  - results/summary.csv          (one row per file × extracted field)
  - results/summary.json         (classification rollup + counts)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Local import — share defaults with pipeline.py
from pipeline import (DEFAULT_TRADE_INVOICES_CONFIG, run_pipeline)


SUPPORTED_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
                  ".heic", ".bmp"}


def _list_files(folder: Path, recursive: bool) -> list[Path]:
    it = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(p for p in it
                  if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS)


def _load_config(path: str | None) -> dict:
    if path is None:
        return DEFAULT_TRADE_INVOICES_CONFIG
    return json.loads(Path(path).read_text())


def _process_one(pdf_path: Path, cfg: dict, out_dir: Path,
                 include_parse: bool) -> dict:
    """Worker: run pipeline, write per-file JSON, return summary row."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (pdf_path.stem + ".result.json")
    t0 = time.time()
    try:
        result = run_pipeline(
            pdf_path,
            classes=cfg["classes"],
            extract_schema=cfg["extract_schema"],
            schema_name=cfg.get("schema_name", "extract_schema"),
            include_parse=include_parse,
        )
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return {
            "file": pdf_path.name,
            "ok": not result.get("errors"),
            "elapsed_s": round(time.time() - t0, 2),
            "classification": result.get("classification", {}),
            "extraction": result.get("extraction", {}),
            "errors": result.get("errors", []),
            "result_path": str(out_path),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "file": pdf_path.name,
            "ok": False,
            "elapsed_s": round(time.time() - t0, 2),
            "classification": {},
            "extraction": {},
            "errors": [{"stage": "worker", "error": str(e)}],
            "result_path": None,
        }


def _write_csv(rows: list[dict], cfg: dict, csv_path: Path) -> None:
    """Wide CSV: one row per file, one column per extract schema property,
    plus one column per classifier class with the page list."""
    extract_props = list(
        cfg["extract_schema"].get("properties", {}).keys()
    )
    class_labels = [c["const"] for c in cfg["classes"]]

    headers = ["file", "ok", "elapsed_s"] \
        + [f"cls.{c}.pages" for c in class_labels] \
        + [f"x.{k}" for k in extract_props] \
        + ["errors"]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            row = [r["file"], r["ok"], r["elapsed_s"]]
            for c in class_labels:
                pages = r["classification"].get(c, {}).get("pages", [])
                row.append(",".join(str(p) for p in pages))
            for k in extract_props:
                v = (r["extraction"] or {}).get(k, "")
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                row.append(v)
            row.append(json.dumps(r["errors"], ensure_ascii=False)
                       if r["errors"] else "")
            w.writerow(row)


def _rollup(rows: list[dict]) -> dict[str, Any]:
    total = len(rows)
    ok = sum(1 for r in rows if r["ok"])
    cls_counts: dict[str, int] = {}
    for r in rows:
        for label, info in (r.get("classification") or {}).items():
            if info.get("pages"):
                cls_counts[label] = cls_counts.get(label, 0) + 1
    return {
        "total_files": total,
        "ok": ok,
        "failed": total - ok,
        "files_per_class": cls_counts,
        "avg_elapsed_s":
            round(sum(r["elapsed_s"] for r in rows) / total, 2)
            if total else 0,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=("Run the headless Studio pipeline over a folder of PDFs "
                     "in parallel."))
    p.add_argument("folder", help="Folder containing PDFs/images.")
    p.add_argument("--schema", help="Path to JSON config (classes + "
                                    "extract_schema). Defaults to Trade "
                                    "invoices baked into pipeline.py.")
    p.add_argument("--out-dir", default="results",
                   help="Output directory. Default: ./results")
    p.add_argument("--csv", help="Path to summary CSV. Default: "
                                 "<out-dir>/summary.csv")
    p.add_argument("--workers", type=int, default=4,
                   help="Parallel API requests. Default: 4")
    p.add_argument("--recursive", action="store_true",
                   help="Recurse into subfolders.")
    p.add_argument("--include-parse", action="store_true",
                   help="Also call Document Parse per file. Slower; useful "
                        "for cache or downstream synth.")
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N files (for smoke tests).")
    args = p.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        return 2

    files = _list_files(folder, args.recursive)
    if args.limit:
        files = files[:args.limit]
    if not files:
        print("No supported files found.", file=sys.stderr)
        return 2

    cfg = _load_config(args.schema)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = Path(args.csv) if args.csv else out_dir / "summary.csv"

    print(f"Processing {len(files)} files with {args.workers} workers…",
          file=sys.stderr)
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_process_one, f, cfg, out_dir,
                             args.include_parse): f
                   for f in files}
        for i, fut in enumerate(as_completed(futures), 1):
            row = fut.result()
            rows.append(row)
            mark = "✓" if row["ok"] else "✗"
            print(f"  [{i}/{len(files)}] {mark} {row['file']} "
                  f"({row['elapsed_s']}s)", file=sys.stderr)

    rows.sort(key=lambda r: r["file"])
    _write_csv(rows, cfg, csv_path)
    rollup = _rollup(rows)
    (out_dir / "summary.json").write_text(
        json.dumps(rollup, ensure_ascii=False, indent=2)
    )

    print(f"\nWrote {len(rows)} per-file results to {out_dir}",
          file=sys.stderr)
    print(f"CSV: {csv_path}", file=sys.stderr)
    print(json.dumps(rollup, ensure_ascii=False, indent=2))
    return 0 if rollup["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
