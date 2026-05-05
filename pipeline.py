#!/usr/bin/env python3
"""
pipeline.py — Headless replacement for the Studio "Trade invoices" agent.

Replaces Chrome/Studio UI entirely. Given a single PDF (and a schema config),
runs Document Parse + Document Classification (split) + Information Extraction
against the Upstage public API and returns one JSON per file containing:

  {
    "file": "...",
    "classification": {                # which pages belong to which class
        "<class>": {"pages": [...]}, ...
    },
    "extraction": { ...8 fields... },  # per the IE schema
    "parse": { ... },                  # optional — full Document Parse output
    "errors": [...]
  }

Use:
    export UPSTAGE_API_KEY=up_...
    python pipeline.py input.pdf --schema schemas/trade_invoices.json

Or call run_pipeline() from your own code / batch.py.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

API_BASE = "https://api.upstage.ai/v1"
DOCUMENT_PARSE_URL = f"{API_BASE}/document-digitization"
CLASSIFY_URL = f"{API_BASE}/document-classification"
EXTRACT_URL = f"{API_BASE}/information-extraction"

PARSE_MODEL = "document-parse"
CLASSIFY_MODEL = "document-classify"
EXTRACT_MODEL = "information-extract"


# ---------- helpers ----------

def _api_key() -> str:
    key = os.environ.get("UPSTAGE_API_KEY")
    if not key:
        raise RuntimeError(
            "UPSTAGE_API_KEY environment variable is not set. "
            "Get a key at https://console.upstage.ai/api-keys, then "
            "`export UPSTAGE_API_KEY=up_...`."
        )
    return key


def _b64_data_url(file_path: Path) -> str:
    """Encode a file as a base64 data URL accepted by Classification/IE."""
    with open(file_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:application/octet-stream;base64,{b64}"


def _post(url: str, *, headers: dict, files: dict | None = None,
          data: dict | None = None, json_body: dict | None = None,
          timeout: int = 180) -> dict:
    """POST helper that raises on non-2xx and parses JSON."""
    resp = requests.post(
        url, headers=headers, files=files, data=data,
        json=json_body, timeout=timeout,
    )
    if not resp.ok:
        raise RuntimeError(
            f"{url} → HTTP {resp.status_code}: {resp.text[:500]}"
        )
    return resp.json()


# ---------- API stages ----------

def parse_document(pdf_path: Path, mode: str = "standard") -> dict:
    """
    Stage 1 — Document Parse.
    Returns the full Document Parse response (HTML + Markdown + elements).

    Optional. Useful for inspection or as input to synth.py. The Trade
    invoices agent in Studio doesn't strictly need this layer — Classify
    and Extract operate directly on the PDF/image — but parsing first
    gives you a structured layout JSON that can be cached and re-used.
    """
    headers = {"Authorization": f"Bearer {_api_key()}"}
    with open(pdf_path, "rb") as f:
        files = {"document": (pdf_path.name, f, "application/pdf")}
        data = {
            "model": PARSE_MODEL,
            "mode": mode,
            "output_formats": json.dumps(["html", "markdown", "text"]),
        }
        return _post(DOCUMENT_PARSE_URL, headers=headers,
                     files=files, data=data)


def classify_document(
    pdf_path: Path,
    classes: list[dict],
    *,
    split: bool = True,
) -> dict:
    """
    Stage 2 — Document Classification.

    `classes` is a list of {"const": "<label>", "description": "<llm-facing>"}
    entries. `split=True` returns per-class page groupings in `choices`,
    matching what Studio's Classify-with-Split mode produces.

    Returns the raw Classification response.
    """
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    body = {
        "model": CLASSIFY_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": _b64_data_url(pdf_path)}}
                ],
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "document-classify",
                "schema": {
                    "type": "string",
                    "oneOf": classes,
                },
            },
        },
    }
    if split:
        body["split"] = True
    return _post(CLASSIFY_URL, headers=headers, json_body=body)


def extract_information(
    pdf_path: Path,
    schema: dict,
    *,
    schema_name: str = "extract_schema",
    split: bool = False,
) -> dict:
    """
    Stage 3 — Information Extraction.

    `schema` is a JSON Schema (object root). The schema must have a flat
    first level (object types not allowed at first level — IE limitation).
    Arrays of objects (line items) ARE allowed at the first level.

    Returns the raw IE response. Caller is responsible for parsing
    response['choices'][0]['message']['content'] (a JSON string).
    """
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    body = {
        "model": EXTRACT_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": _b64_data_url(pdf_path)}}
                ],
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": schema,
            },
        },
    }
    if split:
        body["split"] = True
    return _post(EXTRACT_URL, headers=headers, json_body=body)


# ---------- shape helpers ----------

def _collect_split_classification(classify_response: dict) -> dict[str, dict]:
    """
    Reduce a Classification (split=True) response to:
        { "<class_label>": {"pages": [int...], "confidence": float}, ... }

    Mirrors Studio's per-class subdocument display. Handles both response
    shapes seen in the wild:

      - `page_ranges: [[start, end], ...]`   (current production shape)
      - `pages: [int, ...]`                  (older docs example)
    """
    result: dict[str, dict] = {}
    for choice in classify_response.get("choices", []):
        msg = choice.get("message", {})
        label = msg.get("content")
        if not label:
            continue
        pages: list[int] = []
        confidence: float | None = None
        for tc in (msg.get("tool_calls") or []):
            args = tc.get("function", {}).get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                continue
            # 1) page_ranges (preferred — actual production shape)
            for rng in args.get("page_ranges", []) or []:
                if isinstance(rng, (list, tuple)) and len(rng) == 2 \
                        and all(isinstance(x, int) for x in rng):
                    pages.extend(range(rng[0], rng[1] + 1))
                elif isinstance(rng, int):
                    pages.append(rng)
            # 2) pages (legacy shape)
            for p in args.get("pages", []) or []:
                if isinstance(p, int):
                    pages.append(p)
            # 3) confidence from document_type._value confidence_score
            doc_type = args.get("document_type")
            if isinstance(doc_type, dict):
                cs = doc_type.get("confidence_score")
                if isinstance(cs, (int, float)):
                    confidence = float(cs)
        existing = result.setdefault(
            label, {"pages": [], "confidence": None})
        for p in pages:
            if p not in existing["pages"]:
                existing["pages"].append(p)
        if confidence is not None:
            existing["confidence"] = confidence
    # Sort pages for stable output
    for v in result.values():
        v["pages"].sort()
    return result


def _parse_extraction_content(extract_response: dict) -> dict:
    """Parse the JSON string in choices[0].message.content."""
    try:
        content = extract_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"_error": "no choices in IE response",
                "_raw": extract_response}
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        return {"_error": f"IE content not valid JSON: {e}",
                "_raw_content": content}


# ---------- end-to-end ----------

def run_pipeline(
    pdf_path: str | Path,
    *,
    classes: list[dict],
    extract_schema: dict,
    schema_name: str = "extract_schema",
    include_parse: bool = False,
    parse_mode: str = "standard",
    sleep_between: float = 0.0,
) -> dict:
    """
    Run the full Parse(optional) → Classify → Extract pipeline on one file.

    Returns a unified result dict suitable for serialization.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    out: dict[str, Any] = {
        "file": str(pdf_path),
        "filename": pdf_path.name,
        "size_bytes": pdf_path.stat().st_size,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "errors": [],
    }

    # 1. Optional Parse
    if include_parse:
        try:
            parse_resp = parse_document(pdf_path, mode=parse_mode)
            out["parse"] = parse_resp
            if sleep_between:
                time.sleep(sleep_between)
        except Exception as e:  # noqa: BLE001
            out["errors"].append({"stage": "parse", "error": str(e)})

    # 2. Classify (with split)
    try:
        classify_resp = classify_document(pdf_path, classes=classes,
                                          split=True)
        out["classification_raw"] = classify_resp
        out["classification"] = _collect_split_classification(classify_resp)
        if sleep_between:
            time.sleep(sleep_between)
    except Exception as e:  # noqa: BLE001
        out["errors"].append({"stage": "classify", "error": str(e)})
        out["classification"] = {}

    # 3. Extract on the whole file (IE will pull only fields it finds)
    try:
        extract_resp = extract_information(pdf_path, extract_schema,
                                           schema_name=schema_name)
        out["extraction_raw"] = extract_resp
        out["extraction"] = _parse_extraction_content(extract_resp)
    except Exception as e:  # noqa: BLE001
        out["errors"].append({"stage": "extract", "error": str(e)})
        out["extraction"] = {}

    out["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return out


# ---------- CLI ----------

DEFAULT_TRADE_INVOICES_CONFIG = {
    "schema_name": "trade_invoice",
    "classes": [
        {"const": "commercial_invoice",
         "description": ("Commercial invoice for international trade. "
                         "Header reads 'COMMERCIAL INVOICE'. Has FROM (seller) "
                         "and TO (buyer/ASUNGHMP), INV.NO, DATE, departure/"
                         "destination ports, products table, TOTAL with USD "
                         "currency, payment terms, bank info. Not a packing "
                         "list, not a proforma.")},
        {"const": "packing_list",
         "description": ("Packing list for shipment. Header reads "
                         "'PACKING LIST'. Lists carton numbers, CTNS count, "
                         "item no, quantity, color, carton size, gross weight, "
                         "net weight, CBM. References an invoice number. "
                         "Not fiscal — no totals or VAT.")},
        {"const": "proforma_invoice",
         "description": ("Proforma invoice issued before the final commercial "
                         "invoice. Header reads 'PROFORMA INVOICE' (not "
                         "'COMMERCIAL INVOICE'). Same layout as commercial "
                         "invoice. The reliable distinguishing feature is the "
                         "word 'PROFORMA' in the header.")},
        {"const": "others",
         "description": "Documents that do not belong to the above types."},
    ],
    "extract_schema": {
        "type": "object",
        "properties": {
            "invoice_number":   {"type": "string",
                                 "description": ("INV.NO printed on the "
                                                 "document, e.g. "
                                                 "'JSJT-DC20251125-2' or "
                                                 "'20260101'.")},
            "vendor_name":      {"type": "string",
                                 "description": ("Seller / FROM company. "
                                                 "Often a Chinese "
                                                 "manufacturer name.")},
            "buyer_name":       {"type": "string",
                                 "description": ("Buyer / TO company, "
                                                 "typically 'ASUNGHMP CO.,Ltd' "
                                                 "for this account.")},
            "invoice_date":     {"type": "string",
                                 "description": ("Issue date as written; "
                                                 "normalize to YYYY-MM-DD.")},
            "departure_port":   {"type": "string",
                                 "description": ("DEPARTURE PORT or 'FROM' "
                                                 "city/country, e.g. "
                                                 "'Shanghai'.")},
            "destination_port": {"type": "string",
                                 "description": ("DESTINATION PORT or 'TO' "
                                                 "city/country, e.g. "
                                                 "'Pyongtaek'.")},
            "currency":         {"type": "string",
                                 "description": ("ISO 4217 currency code. "
                                                 "Default to USD if FOB or "
                                                 "'$' is shown.")},
            "total_amount":     {"type": "string",
                                 "description": ("Grand TOTAL of the invoice "
                                                 "as shown, e.g. '$7,920.00'.")},
        },
        "required": ["invoice_number", "vendor_name", "buyer_name",
                     "invoice_date", "total_amount"],
    },
}


def _load_config(path: str | None) -> dict:
    if path is None:
        return DEFAULT_TRADE_INVOICES_CONFIG
    with open(path) as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=("Headless Studio agent: Parse + Classify + Extract "
                     "on a single PDF using the Upstage public APIs."))
    p.add_argument("pdf", help="Path to the input PDF.")
    p.add_argument("--schema", help="Path to a JSON config with 'classes' "
                                    "and 'extract_schema'. Defaults to the "
                                    "Trade invoices config baked into this "
                                    "script.")
    p.add_argument("--out", help="Where to write the result JSON. "
                                 "Default: <pdf>.result.json next to input.")
    p.add_argument("--include-parse", action="store_true",
                   help="Also call Document Parse and include the response.")
    p.add_argument("--parse-mode", default="standard",
                   choices=["standard", "enhanced", "auto"])
    args = p.parse_args(argv)

    cfg = _load_config(args.schema)
    pdf_path = Path(args.pdf)
    out_path = Path(args.out) if args.out \
        else pdf_path.with_suffix(pdf_path.suffix + ".result.json")

    result = run_pipeline(
        pdf_path,
        classes=cfg["classes"],
        extract_schema=cfg["extract_schema"],
        schema_name=cfg.get("schema_name", "extract_schema"),
        include_parse=args.include_parse,
        parse_mode=args.parse_mode,
    )

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    # Lean printable summary
    print(json.dumps({
        "file": result["filename"],
        "classification": result.get("classification"),
        "extraction": result.get("extraction"),
        "errors": result.get("errors"),
    }, ensure_ascii=False, indent=2))
    print(f"\nFull result written to: {out_path}", file=sys.stderr)
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    sys.exit(main())
