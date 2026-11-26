"""Centralized runtime configuration, whitespace-safe .env loader, and engineering constants."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Optional

DEFAULT_HF_DATASET_REPO = "Lakshya7773/india-telecom-spatial-v2"
DEFAULT_DB_LOCAL_PATH = "/tmp/all_india_spatial.sqlite"
DEFAULT_DATA_CACHE_DIR = "./data_cache"
PEAK_RSS_LIMIT_MB = 1536.0

_UNQUOTED_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")


def _strip_matching_quotes(raw_val: str) -> str:
    """Parse quoted env values with basic escape handling, stripping trailing comments."""
    quote = raw_val[0]
    chars: list[str] = []
    escaped = False
    for ch in raw_val[1:]:
        if escaped:
            if ch == "n":
                chars.append("\n")
            elif ch == "t":
                chars.append("\t")
            else:
                chars.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote == '"':
            escaped = True
            continue
        if ch == quote:
            return "".join(chars)
        chars.append(ch)
    # Unterminated quote: fallback to stripped string without opening quote
    return raw_val[1:].strip()


def parse_env_line(line: str) -> Optional[tuple[str, str]]:
    """
    Parse a single .env / .env.txt line without external dependencies.
    Handles blank lines, `#` comments, `export ` prefixes, spaces around `=`,
    single/double quotes, and inline `#` comments on unquoted values.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    if "=" not in stripped:
        return None

    key, raw_val = stripped.split("=", 1)
    key = key.strip()
    if not key or key.startswith("#"):
        return None

    raw_val = raw_val.strip()
    if not raw_val:
        return key, ""

    if raw_val[0] in ('"', "'"):
        return key, _strip_matching_quotes(raw_val)

    cleaned = _UNQUOTED_INLINE_COMMENT_RE.sub("", raw_val).strip()
    return key, cleaned


def load_env_file(path: Path, *, override: bool = False) -> dict[str, str]:
    """Load key=value pairs from a single env file into os.environ."""
    parsed: dict[str, str] = {}
    if not path.is_file():
        return parsed

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        item = parse_env_line(raw_line)
        if item is None:
            continue
        key, value = item
        parsed[key] = value
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)
    return parsed


def load_env_files(base_dir: Optional[Path] = None) -> dict[str, str]:
    """
    Load `.env.txt` and `.env` without overwriting pre-existing `os.environ` keys.
    `.env` values take precedence over `.env.txt` when neither is in `os.environ`.
    """
    root = base_dir if base_dir is not None else Path(__file__).resolve().parent.parent
    merged: dict[str, str] = {}

    for filename in (".env.txt", ".env"):
        file_path = root / filename
        if not file_path.is_file():
            continue
        for raw_line in file_path.read_text(encoding="utf-8").splitlines():
            item = parse_env_line(raw_line)
            if item is not None:
                merged[item[0]] = item[1]

    for key, value in merged.items():
        os.environ.setdefault(key, value)
    return merged


# Populate defaults at import time without clobbering pre-set env vars (e.g. conftest.py)
load_env_files()


def get_hf_token() -> str:
    return os.environ.get("HF_TOKEN", "").strip()


def get_hf_dataset_repo() -> str:
    return os.environ.get("HF_DATASET_REPO", DEFAULT_HF_DATASET_REPO).strip() or DEFAULT_HF_DATASET_REPO


def get_db_local_path() -> str:
    return os.environ.get("DB_LOCAL_PATH", DEFAULT_DB_LOCAL_PATH).strip() or DEFAULT_DB_LOCAL_PATH


def get_data_cache_dir() -> Path:
    raw = os.environ.get("DATA_CACHE_DIR", DEFAULT_DATA_CACHE_DIR).strip() or DEFAULT_DATA_CACHE_DIR
    return Path(raw)


def get_rasters() -> dict[str, str]:
    base_url = f"https://huggingface.co/datasets/{get_hf_dataset_repo()}/resolve/main"
    return {
        "dem_elevation_m": f"/vsicurl/{base_url}/dem/india_dem_30m.cog.tif",
        "population_100m": f"/vsicurl/{base_url}/ind_pop_2027_CN_100m_R2025A_v1.tif",
        "solar_pvout_kwh_kwp": (
            f"/vsicurl/{base_url}/India_GISdata_LTAy_AvgDailyTotals_GlobalSolarAtlas-v2_GEOTIFF/PVOUT.tif"
        ),
    }


# Module-level compatibility constants (dynamic getters above are canonical at runtime)
HF_TOKEN = get_hf_token()
HF_DATASET_REPO = get_hf_dataset_repo()
DB_LOCAL_PATH = get_db_local_path()
DATA_CACHE_DIR = get_data_cache_dir()
RASTERS = get_rasters()

# ---------------------------------------------------------------------------
# Indian Telecom Typologies, Power / Solar / LiFePO4 / Diesel / CO2 Constants
# ---------------------------------------------------------------------------
TYPOLOGY_SPECS: dict[str, dict[str, Any]] = {
    "Macro GBT": {
        "load_kw": 2.0,
        "default_autonomy_h": 12,
        "dg_baseline_share": 0.45,
        "tenancy_capacity_max": 3,
        "mast_height_m": 40.0,
    },
    "Urban RTT/Monopole": {
        "load_kw": 1.5,
        "default_autonomy_h": 10,
        "dg_baseline_share": 0.30,
        "tenancy_capacity_max": 2,
        "mast_height_m": 25.0,
    },
    "5G Street-Furniture Small Cell": {
        "load_kw": 0.35,
        "default_autonomy_h": 8,
        "dg_baseline_share": 0.15,
        "tenancy_capacity_max": 2,
        "mast_height_m": 12.0,
    },
    "Industrial Private 5G / Edge Node": {
        "load_kw": 2.5,
        "default_autonomy_h": 12,
        "dg_baseline_share": 0.35,
        "tenancy_capacity_max": 3,
        "mast_height_m": 30.0,
    },
}

DEFAULT_SOLAR_PVOUT_KWH_KWP_DAY = 4.8
SOLAR_PERFORMANCE_RATIO = 0.78
LIFEPO4_DOD = 0.90
WEAK_GRID_POWER_THRESHOLD_M = 2500.0
WEAK_GRID_AUTONOMY_HOURS = 24
DG_DIESEL_LITERS_PER_KWH = 0.35
DG_WEAK_GRID_POWER_THRESHOLD_M = 3000.0
DG_WEAK_GRID_SHARE_ADD = 0.20
DG_MAX_SHARE = 0.75
DIESEL_CO2_KG_PER_LITER = 2.68
DIESEL_COST_INR_PER_LITER = 92.0

BEYOND_TENANCY_THRESHOLDS: dict[str, float] = {
    "edge_base_score": 30.0,
    "ev_base_score": 20.0,
    "suitable_min_score": 60,
    "high_tier_min_score": 70,
    "moderate_tier_min_score": 45,
}

# ---------------------------------------------------------------------------
# Statutory & Right-of-Way (RoW) Proximity Thresholds (m)
# ---------------------------------------------------------------------------
STATUTORY_CATEGORIES: dict[str, dict[str, Any]] = {
    "aviation_buffer": {
        "keywords": ("airport", "aerodrome", "helipad", "runway", "airfield", "aviation"),
        "hold_m": 1500.0,
        "warn_m": 4000.0,
        "authority": "AAI / DGCA (SACFA Height Clearance)",
    },
    "railways": {
        "keywords": ("railway", "rail", "train", "station", "metro", "track"),
        "hold_m": 50.0,
        "warn_m": 250.0,
        "authority": "Indian Railways / Metro Rail Corp RoW",
    },
    "highways": {
        "keywords": ("highway", "motorway", "trunk", "expressway", "nh-", "nh "),
        "hold_m": 30.0,
        "warn_m": 150.0,
        "authority": "NHAI / State PWD (PM GatiShakti RoW)",
    },
    "waterbodies": {
        "keywords": ("river", "lake", "pond", "canal", "reservoir", "water", "wetland", "dam"),
        "hold_m": 75.0,
        "warn_m": 250.0,
        "authority": "State Irrigation / Water Resources / CRZ",
    },
    "forest_eco_zones": {
        "keywords": ("forest", "sanctuary", "wildlife", "national_park", "protected", "reserve", "eco"),
        "hold_m": 300.0,
        "warn_m": 1000.0,
        "authority": "MoEFCC / Parivesh / State Forest Dept",
    },
}

# ---------------------------------------------------------------------------
# FAO Soil Risk Multipliers & Foundation Reference Parameters
# ---------------------------------------------------------------------------
SOIL_RISK_MULTIPLIER: dict[str, float] = {
    "VERTISOLS": 1.35,   # Black cotton / expansive clay
    "GLEYSOLS": 1.40,    # Waterlogged / poor drainage
    "HISTOSOLS": 1.50,   # Peat / organic
    "FLUVISOLS": 1.25,   # Alluvial / flood-prone
    "SOLONCHAKS": 1.20,  # Saline
    "CAMBISOLS": 1.00,
    "LUVISOLS": 1.00,
    "LITHOSOLS": 0.90,   # Shallow rock - good bearing
    "REGOSOLS": 1.05,
}
DEFAULT_SOIL_RISK_MULTIPLIER = 1.05
DEFAULT_FALLBACK_SLOPE_PCT = 2.0

_FAO_DSMW_PREFIX_MAP: dict[str, str] = {
    "VC": "VERTISOLS", "VP": "VERTISOLS", "V": "VERTISOLS",
    "GL": "GLEYSOLS", "GE": "GLEYSOLS", "GD": "GLEYSOLS", "GM": "GLEYSOLS", "G": "GLEYSOLS",
    "HS": "HISTOSOLS", "OD": "HISTOSOLS", "OE": "HISTOSOLS", "O": "HISTOSOLS",
    "FL": "FLUVISOLS", "JE": "FLUVISOLS", "JC": "FLUVISOLS", "JD": "FLUVISOLS", "J": "FLUVISOLS",
    "SC": "SOLONCHAKS", "ZG": "SOLONCHAKS", "Z": "SOLONCHAKS",
    "BC": "CAMBISOLS", "BK": "CAMBISOLS", "BE": "CAMBISOLS", "BD": "CAMBISOLS", "BH": "CAMBISOLS", "B": "CAMBISOLS",
    "LV": "LUVISOLS", "LC": "LUVISOLS", "LF": "LUVISOLS", "LG": "LUVISOLS", "LO": "LUVISOLS", "L": "LUVISOLS",
    "LP": "LITHOSOLS", "I": "LITHOSOLS",
    "RG": "REGOSOLS", "RE": "REGOSOLS", "RC": "REGOSOLS", "RD": "REGOSOLS", "R": "REGOSOLS",
}


def normalize_fao_soil_class(raw_soil: Optional[str]) -> str:
    """Map raw FAO DSMW codes (e.g., 'VC:VC43-3AB') or class names to canonical soil classes."""
    key = (raw_soil or "").strip().upper()
    if not key or key == "UNKNOWN":
        return "CAMBISOLS"
    if key in SOIL_RISK_MULTIPLIER:
        return key
    prefix = key.split(":", 1)[0].strip()
    if prefix in SOIL_RISK_MULTIPLIER:
        return prefix
    for code_len in (2, 1):
        if len(prefix) >= code_len and prefix[:code_len] in _FAO_DSMW_PREFIX_MAP:
            return _FAO_DSMW_PREFIX_MAP[prefix[:code_len]]
    return key

SOIL_FOUNDATION_SPEC: dict[str, dict[str, Any]] = {
    "VERTISOLS": {
        "bearing_kpa": 110.0,
        "foundation_type": "Under-reamed Pile / Deep Raft (Expansive Clay Mitigation)",
        "depth_m": 4.5,
    },
    "GLEYSOLS": {
        "bearing_kpa": 95.0,
        "foundation_type": "Driven RCC Pile Foundation with Waterproofing",
        "depth_m": 5.0,
    },
    "HISTOSOLS": {
        "bearing_kpa": 75.0,
        "foundation_type": "End-Bearing Deep Pile to Hard Stratum",
        "depth_m": 6.0,
    },
    "FLUVISOLS": {
        "bearing_kpa": 125.0,
        "foundation_type": "Raised Plinth RCC Raft / Pile Foundation",
        "depth_m": 3.8,
    },
    "SOLONCHAKS": {
        "bearing_kpa": 130.0,
        "foundation_type": "Sulfate-Resistant Cement (SRC) Isolated Footing",
        "depth_m": 3.2,
    },
    "CAMBISOLS": {
        "bearing_kpa": 165.0,
        "foundation_type": "Isolated RCC Pad & Pedestal Footing",
        "depth_m": 2.8,
    },
    "LUVISOLS": {
        "bearing_kpa": 160.0,
        "foundation_type": "Isolated RCC Pad & Pedestal Footing",
        "depth_m": 2.8,
    },
    "LITHOSOLS": {
        "bearing_kpa": 250.0,
        "foundation_type": "Rock-Anchored Shallow Pad Footing",
        "depth_m": 1.8,
    },
    "REGOSOLS": {
        "bearing_kpa": 140.0,
        "foundation_type": "Reinforced Stepped RCC Footing",
        "depth_m": 3.0,
    },
}

# ---------------------------------------------------------------------------
# IS 875 (Part 3):2015 Basic Wind Speed Zones (m/s) & State Lookup Map
# ---------------------------------------------------------------------------
IS875_WIND_ZONES: dict[str, float] = {
    "I": 33.0,
    "II": 39.0,
    "III": 44.0,
    "IV": 47.0,
    "V": 50.0,
    "VI": 55.0,
}
DEFAULT_WIND_ZONE = "III"

STATE_WIND_ZONE_MAP: dict[str, str] = {
    "ANDAMAN AND NICOBAR ISLANDS": "V",
    "ANDHRA PRADESH": "V",
    "ARUNACHAL PRADESH": "V",
    "ASSAM": "V",
    "BIHAR": "IV",
    "CHANDIGARH": "IV",
    "CHHATTISGARH": "III",
    "DADRA AND NAGAR HAVELI AND DAMAN AND DIU": "III",
    "DELHI": "IV",
    "GOA": "III",
    "GUJARAT": "III",
    "HARYANA": "IV",
    "HIMACHAL PRADESH": "III",
    "JAMMU AND KASHMIR": "III",
    "JHARKHAND": "III",
    "KARNATAKA": "I",
    "KERALA": "III",
    "LADAKH": "III",
    "LAKSHADWEEP": "IV",
    "MADHYA PRADESH": "III",
    "MAHARASHTRA": "III",
    "MANIPUR": "V",
    "MEGHALAYA": "V",
    "MIZORAM": "V",
    "NAGALAND": "V",
    "ODISHA": "V",
    "PUDUCHERRY": "V",
    "PUNJAB": "IV",
    "RAJASTHAN": "IV",
    "SIKKIM": "IV",
    "TAMIL NADU": "IV",
    "TELANGANA": "III",
    "TRIPURA": "V",
    "UTTAR PRADESH": "IV",
    "UTTARAKHAND": "IV",
    "WEST BENGAL": "V",
}

# ---------------------------------------------------------------------------
# Backhaul FTTT & Microwave 60% Fresnel LoS Constants
# ---------------------------------------------------------------------------
BANDS_GHZ: dict[str, float] = {
    "6_GHz": 6.0,
    "15_GHz": 15.0,
    "80_GHz_E_band": 80.0,
}
EFFECTIVE_EARTH_RADIUS_M = (4.0 / 3.0) * 6371000.0
FTTT_ROUTE_FACTOR = 1.25
FTTT_COST_PER_KM_LAKH = 4.5
FTTT_TERMINATION_LAKH = 0.8
FRESNEL_MIN_CLEARANCE_RATIO = 0.6
FRESNEL_CONST = 17.32
E_BAND_80GHZ_MAX_RANGE_M = 6000.0
FIBER_MAX_FEASIBLE_TRENCH_M = 5000.0
FIBER_PRIMARY_THRESHOLD_M = 1500.0
DEFAULT_ANTENNA_HEIGHT_M = 35.0
LOS_PROFILE_POINTS = 30

# ---------------------------------------------------------------------------
# Indian Telecom Financial BOQ & Multi-Tenant IRR Constants
# ---------------------------------------------------------------------------
TYPOLOGY_FINANCIAL_BASE: dict[str, dict[str, float | int]] = {
    "Macro GBT": {
        "civil_lakh": 7.5,
        "structure_lakh": 12.5,
        "lease_inr": 16000,
        "om_inr": 10000,
        "tenant_rev_inr": 48000,
    },
    "Urban RTT/Monopole": {
        "civil_lakh": 4.8,
        "structure_lakh": 8.0,
        "lease_inr": 24000,
        "om_inr": 9500,
        "tenant_rev_inr": 40000,
    },
    "5G Street-Furniture Small Cell": {
        "civil_lakh": 1.4,
        "structure_lakh": 2.5,
        "lease_inr": 7500,
        "om_inr": 3500,
        "tenant_rev_inr": 16000,
    },
    "Industrial Private 5G / Edge Node": {
        "civil_lakh": 8.5,
        "structure_lakh": 10.5,
        "lease_inr": 28000,
        "om_inr": 12000,
        "tenant_rev_inr": 62000,
    },
}

SLOPE_BASELINE_PCT = 2.0
SLOPE_CAPEX_PENALTY_PER_PCT = 0.04
SOLAR_CAPEX_LAKH_PER_KWP = 0.52
LIFEPO4_CAPEX_LAKH_PER_KWH = 0.16
POWER_BOS_FIXED_LAKH = 1.2
DEFAULT_FTTT_FALLBACK_LAKH = 5.0
HYBRID_FTTT_FALLBACK_LAKH = 4.5
HYBRID_MW_ADDON_LAKH = 3.8
MW_80GHZ_CAPEX_LAKH = 4.2
MW_STD_CAPEX_LAKH = 6.8
GRID_SOLAR_OPEX_STANDARD_INR = 4500
GRID_SOLAR_OPEX_SMALL_CELL_INR = 1800
BACKHAUL_OPEX_FIBER_INR = 6000
BACKHAUL_OPEX_MW_INR = 8500
ANCILLARY_REVENUE_HIGH_INR = 9000
ANCILLARY_REVENUE_MED_INR = 4000
EDGE_EV_DEFAULT_ADDON_CAPEX_LAKH = 3.5
IRR_HORIZON_YEARS = 10
REVENUE_ESCALATION_RATE = 0.03
OPEX_ESCALATION_RATE = 0.04
FIBER_SAVED_LAKH_PER_M = 0.0045
CIVIL_SLOPE_SAVING_FACTOR = 0.35
TENANCY_SCENARIOS: tuple[tuple[str, float], ...] = (
    ("1.0x", 1.0),
    ("1.5x", 1.5),
    ("2.0x", 2.0),
)
