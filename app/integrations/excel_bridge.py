"""
Integration: Schema-preserving fuzzy CSV/XLSX column mapper.

Normalises uploaded headers and maps them to canonical field names
(lat, lon, site_id, search_radius_m) using difflib fuzzy matching,
while preserving ALL original columns in the output.
"""

from __future__ import annotations

import difflib
import unicodedata
from typing import Any, Optional

from app.schemas.feasibility_v2 import BatchColumnMappingSummary

# ---------------------------------------------------------------------------
# Canonical field definitions
# ---------------------------------------------------------------------------
CANONICAL_FIELDS: dict[str, list[str]] = {
    "lat": [
        "lat", "latitude", "lat_deg", "y", "site_lat", "centroid_lat",
        "point_lat", "gps_lat", "decimal_lat",
    ],
    "lon": [
        "lon", "lng", "long", "longitude", "lon_deg", "x", "site_lon",
        "centroid_lon", "point_lon", "gps_lon", "decimal_lon",
    ],
    "site_id": [
        "site_id", "siteid", "id", "site_name", "name", "site", "tower_id",
        "tower_name", "circle_id", "ring_id", "candidate_id",
    ],
    "search_radius_m": [
        "search_radius_m", "radius_m", "ring_radius_m", "search_radius",
        "radius", "ring_radius", "buffer_m", "radius_meters",
    ],
}

NUMERIC_FIELDS = {"lat", "lon", "search_radius_m"}
DEFAULT_SEARCH_RADIUS_M = 500.0


def _normalise(text: str) -> str:
    """Lower-case, strip accents, strip non-alphanumeric except underscore."""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    return "".join(c if c.isalnum() or c == "_" else "_" for c in ascii_str.lower()).strip("_")


def _fuzzy_match(norm_header: str, cutoff: float = 0.72) -> Optional[str]:
    """Return canonical field name for norm_header using difflib, or None."""
    best_score = 0.0
    best_canon: Optional[str] = None
    for canon, aliases in CANONICAL_FIELDS.items():
        for alias in aliases:
            score = difflib.SequenceMatcher(None, norm_header, alias).ratio()
            if score > best_score:
                best_score = score
                best_canon = canon
    if best_score >= cutoff:
        return best_canon
    return None


def map_columns(
    headers: list[str],
    cutoff: float = 0.72,
) -> BatchColumnMappingSummary:
    """
    Map raw CSV/XLSX headers to canonical field names.

    Returns BatchColumnMappingSummary with:
      - detected_mapping: {canonical_name → original_header or None}
      - unmapped_headers: original headers that couldn't be mapped to any canonical field
      - preserved_headers: all original headers (for schema preservation)
    """
    detected: dict[str, Optional[str]] = {canon: None for canon in CANONICAL_FIELDS}
    already_assigned: set[str] = set()

    for header in headers:
        norm = _normalise(header)
        canon = _fuzzy_match(norm, cutoff=cutoff)
        if canon and detected[canon] is None and header not in already_assigned:
            detected[canon] = header
            already_assigned.add(header)

    mapped_originals = set(v for v in detected.values() if v is not None)
    unmapped = [h for h in headers if h not in mapped_originals]

    return BatchColumnMappingSummary(
        detected_mapping=detected,
        unmapped_headers=unmapped,
        preserved_headers=list(headers),
    )


def extract_row(
    row: dict[str, Any],
    mapping: BatchColumnMappingSummary,
    row_index: int = 0,
) -> dict[str, Any]:
    """
    Extract canonical fields from a data row using the column mapping.
    Returns dict with canonical keys + all original keys preserved.
    Returns None values for unmapped or unparseable canonical fields.
    """
    result: dict[str, Any] = dict(row)  # preserve all original columns

    dm = mapping.detected_mapping
    for canon, orig_header in dm.items():
        if orig_header is None:
            # Apply defaults for missing fields
            if canon == "search_radius_m":
                result[canon] = DEFAULT_SEARCH_RADIUS_M
            elif canon == "site_id":
                result[canon] = f"RING-{row_index + 1:04d}"
            else:
                result[canon] = None
            continue

        raw_val = row.get(orig_header)
        if raw_val is None or str(raw_val).strip() == "":
            result[canon] = DEFAULT_SEARCH_RADIUS_M if canon == "search_radius_m" else None
            continue

        if canon in NUMERIC_FIELDS:
            try:
                result[canon] = float(str(raw_val).replace(",", "").strip())
            except (ValueError, TypeError):
                result[canon] = None
        else:
            result[canon] = str(raw_val).strip()

    # Fallback site_id
    if not result.get("site_id"):
        result["site_id"] = f"RING-{row_index + 1:04d}"

    return result


def parse_csv_bytes(raw_bytes: bytes, encoding: str = "utf-8-sig") -> tuple[list[str], list[dict[str, Any]]]:
    """Parse raw CSV bytes into (headers, rows). Returns empty lists on error."""
    import csv
    import io
    try:
        text = raw_bytes.decode(encoding, errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        headers = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
        return headers, rows
    except Exception:
        return [], []


def parse_xlsx_bytes(raw_bytes: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    """Parse raw XLSX bytes into (headers, rows) using openpyxl."""
    import io
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        raw_headers = next(rows_iter, None)
        if raw_headers is None:
            return [], []
        headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(raw_headers)]
        rows = []
        for raw_row in rows_iter:
            if all(v is None for v in raw_row):
                continue  # skip blank rows
            rows.append({h: v for h, v in zip(headers, raw_row)})
        wb.close()
        return headers, rows
    except Exception:
        return [], []
