import csv
import gc
import io
from typing import Optional

from fpdf import FPDF
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from app.modules.backhaul_los import analyze_backhaul_and_los
from app.modules.enterprise_2030 import analyze_enterprise_2030
from app.modules.financial_boq import calculate_financial_boq
from app.modules.ring_optimizer import optimize_search_ring
from app.spatial_core import check_feasibility

MAX_BATCH_RINGS = 500


def parse_batch_upload(filename: str, content: bytes) -> list[dict]:
    """Parse CSV or Excel (.xlsx) upload into normalized search-ring rows (max 500)."""
    lower = (filename or "").lower()
    raw_rows: list[dict] = []

    if lower.endswith(".xlsx") or lower.endswith(".xlsm"):
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        headers = [str(h).strip().lower() if h is not None else "" for h in next(rows_iter, [])]
        for row_vals in rows_iter:
            if not any(v is not None for v in row_vals):
                continue
            raw_rows.append(dict(zip(headers, row_vals)))
        wb.close()
    else:
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        for r in reader:
            raw_rows.append({(k or "").strip().lower(): v for k, v in r.items()})

    if len(raw_rows) > MAX_BATCH_RINGS:
        raise ValueError(f"Batch upload exceeds maximum of {MAX_BATCH_RINGS} search rings.")

    parsed: list[dict] = []
    for idx, r in enumerate(raw_rows, start=1):
        lat_val = r.get("lat") or r.get("latitude")
        lon_val = r.get("lon") or r.get("lng") or r.get("longitude")
        if lat_val is None or lon_val is None or str(lat_val).strip() == "" or str(lon_val).strip() == "":
            continue
        site_id = str(r.get("site_id") or r.get("id") or r.get("name") or f"RING-{idx:03d}").strip()
        radius_m = float(r.get("radius_m") or 500.0)
        parsed.append(
            {
                "site_id": site_id,
                "lat": float(lat_val),
                "lon": float(lon_val),
                "radius_m": radius_m,
            }
        )

    if not parsed:
        raise ValueError("No valid rows with lat/lon columns found in uploaded file.")

    return parsed


def _classify_triage(
    statutory_status: str,
    irr_15x: Optional[float],
    nearest_ofc_m: Optional[float],
    nearest_power_m: Optional[float],
) -> tuple[str, int]:
    """Return (Green|Amber|Red, rank_score 0-100)."""
    score = 50.0
    if irr_15x is not None:
        score += min(30.0, max(-20.0, (irr_15x - 10.0) * 2.0))
    if nearest_ofc_m is not None:
        score += 12.0 if nearest_ofc_m <= 1500 else (6.0 if nearest_ofc_m <= 4000 else 0.0)
    if nearest_power_m is not None:
        score += 8.0 if nearest_power_m <= 2000 else (4.0 if nearest_power_m <= 5000 else 0.0)

    if statutory_status == "HOLD_CLEARANCE_REQUIRED":
        score -= 35.0
        status = "Red"
    elif irr_15x is None or irr_15x < 8.0:
        status = "Red"
    elif statutory_status == "CLEAR" and irr_15x >= 15.0:
        status = "Green"
    elif irr_15x >= 18.0 and statutory_status == "CONDITIONAL_ROW":
        status = "Green"
    else:
        status = "Amber"

    return status, max(0, min(100, int(round(score))))


def run_batch_triage(rows: list[dict], fast_mode: bool = False) -> dict:
    """
    Memory-safe sequential/chunked scoring for up to 500 search rings.
    Processes in chunks of 25 with explicit gc.collect() to stay well under 1.5 GB RAM.
    """
    results: list[dict] = []
    chunk_size = 25

    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        for row in chunk:
            site_id = row["site_id"]
            lat = row["lat"]
            lon = row["lon"]
            radius_m = row.get("radius_m", 500.0)

            base = check_feasibility(lat, lon, radius_km=5.0)
            ring = optimize_search_ring(
                lat,
                lon,
                radius_m=radius_m,
                grid_points=9 if fast_mode else 9,
                fao_soil=base["site_metrics"].get("fao_soil"),
            )
            opt_lat = ring["recommended_pin"]["lat"]
            opt_lon = ring["recommended_pin"]["lon"]

            ent = analyze_enterprise_2030(
                population_1km=base["site_metrics"].get("population_1km_catchment"),
                towers_in_radius=base["infrastructure"]["towers_in_radius"],
                nearest_power_m=base["infrastructure"]["nearest_power_substation_m"],
                nearest_ofc_m=ring["recommended_pin"]["bharatnet_ofc_m"],
                solar_pvout_kwh_kwp=base["site_metrics"].get("solar_pvout_kwh_kwp"),
                exclusions=base["nearest_exclusions"],
            )

            ofc_m = ring["recommended_pin"]["bharatnet_ofc_m"]
            fttt_lakh = round((ofc_m * 1.25 / 1000.0) * 4.5 + 0.8, 2) if ofc_m is not None else 5.0
            backhaul_mode = "FIBER_FTTT_PRIMARY" if (ofc_m is not None and ofc_m <= 2500.0) else "MW_15GHZ_6GHZ"

            fin = calculate_financial_boq(
                typology=ent["typology"]["asset_typology"],
                slope_pct=ring["recommended_pin"]["slope_pct"],
                soil_risk_multiplier=ring["deltas"]["soil_risk_multiplier"],
                solar_pv_kwp=ent["zero_diesel_esg"]["recommended_solar_pv_kwp"],
                battery_kwh=ent["zero_diesel_esg"]["lifepo4_battery_kwh"],
                fttt_capex_lakh=fttt_lakh,
                backhaul_mode=backhaul_mode,
                beyond_tenancy_score=ent["beyond_tenancy"]["composite_beyond_tenancy_score"],
            )

            irr_15x = fin["irr_sensitivity"]["1.5x"]["irr_10yr_pct"]
            triage_status, rank_score = _classify_triage(
                statutory_status=ent["statutory_row_gate"]["overall_gate_status"],
                irr_15x=irr_15x,
                nearest_ofc_m=ofc_m,
                nearest_power_m=base["infrastructure"]["nearest_power_substation_m"],
            )

            results.append(
                {
                    "site_id": site_id,
                    "status": triage_status,
                    "rank_score": rank_score,
                    "input_lat": lat,
                    "input_lon": lon,
                    "opt_lat": opt_lat,
                    "opt_lon": opt_lon,
                    "meters_shifted": ring["deltas"]["meters_shifted"],
                    "elevation_m": ring["recommended_pin"]["elevation_m"],
                    "capex_saved_lakh": ring["deltas"]["capex_saved_lakh"],
                    "state": base["site_metrics"]["state"],
                    "typology": ent["typology"]["asset_typology"],
                    "statutory_gate": ent["statutory_row_gate"]["overall_gate_status"],
                    "bharatnet_ofc_m": ofc_m,
                    "nearest_power_m": base["infrastructure"]["nearest_power_substation_m"],
                    "solar_pv_kwp": ent["zero_diesel_esg"]["recommended_solar_pv_kwp"],
                    "co2_offset_tpy": ent["zero_diesel_esg"]["co2_offset_tons_per_year"],
                    "beyond_tenancy_score": ent["beyond_tenancy"]["composite_beyond_tenancy_score"],
                    "total_capex_lakh": fin["capex_boq_lakh"]["total_capex_lakh"],
                    "monthly_opex_inr": fin["opex_monthly_inr"]["total_monthly_opex_inr"],
                    "irr_1_0x_pct": fin["irr_sensitivity"]["1.0x"]["irr_10yr_pct"],
                    "irr_1_5x_pct": irr_15x,
                    "irr_2_0x_pct": fin["irr_sensitivity"]["2.0x"]["irr_10yr_pct"],
                }
            )
        gc.collect()

    status_order = {"Green": 0, "Amber": 1, "Red": 2}
    results.sort(key=lambda x: (status_order[x["status"]], -x["rank_score"]))

    green_cnt = sum(1 for r in results if r["status"] == "Green")
    amber_cnt = sum(1 for r in results if r["status"] == "Amber")
    red_cnt = sum(1 for r in results if r["status"] == "Red")
    total_capex_cr = round(sum(r["total_capex_lakh"] for r in results) / 100.0, 2)
    irrs = [r["irr_1_5x_pct"] for r in results if r["irr_1_5x_pct"] is not None]
    avg_irr = round(sum(irrs) / len(irrs), 2) if irrs else 0.0
    total_co2 = round(sum(r["co2_offset_tpy"] for r in results), 2)

    return {
        "summary": {
            "total_rings": len(results),
            "green_count": green_cnt,
            "amber_count": amber_cnt,
            "red_count": red_cnt,
            "total_capex_crore": total_capex_cr,
            "avg_irr_1_5x_pct": avg_irr,
            "total_co2_offset_tpy": total_co2,
        },
        "sites": results,
    }


def build_triage_workbook_bytes(triage_result: dict) -> bytes:
    """Create a ranked .xlsx workbook with Summary + Ranked Sites sheets."""
    wb = openpyxl.Workbook()

    # Sheet 1: Summary
    ws_sum = wb.active
    ws_sum.title = "Summary"
    header_fill = PatternFill("solid", fgColor="0F172A")
    header_font = Font(bold=True, color="FFFFFF", size=11)

    ws_sum["A1"] = "2030 Enterprise Tower Feasibility - Batch RFP Triage Summary"
    ws_sum["A1"].font = Font(bold=True, size=14, color="0F172A")

    summary = triage_result["summary"]
    kpi_rows = [
        ("Total Search Rings Evaluated", summary["total_rings"]),
        ("Green (Bankable / Ready)", summary["green_count"]),
        ("Amber (Conditional / Optimize)", summary["amber_count"]),
        ("Red (Hold / Unviable)", summary["red_count"]),
        ("Total Estimated Capex (INR Crore)", summary["total_capex_crore"]),
        ("Average 1.5x Tenancy 10Y IRR (%)", summary["avg_irr_1_5x_pct"]),
        ("Total Zero-Diesel CO2 Offset (Tons/Yr)", summary["total_co2_offset_tpy"]),
    ]
    ws_sum.append([])
    ws_sum.append(["Metric", "Value"])
    for cell in ws_sum[3]:
        cell.fill = header_fill
        cell.font = header_font
    for k, v in kpi_rows:
        ws_sum.append([k, v])
    ws_sum.column_dimensions["A"].width = 40
    ws_sum.column_dimensions["B"].width = 20

    # Sheet 2: Ranked Sites
    ws_sites = wb.create_sheet("Ranked Sites")
    columns = [
        "Rank",
        "Site ID",
        "Status",
        "Score",
        "State",
        "Input Lat",
        "Input Lon",
        "Optimal Lat",
        "Optimal Lon",
        "Shifted (m)",
        "Elev (m)",
        "Capex Saved (INR L)",
        "2030 Typology",
        "Statutory Gate",
        "BharatNet OFC (m)",
        "Power Sub (m)",
        "Solar PV (kWp)",
        "CO2 Offset (TPY)",
        "Beyond-Tenancy",
        "Total Capex (INR L)",
        "Monthly Opex (INR)",
        "IRR 1.0x (%)",
        "IRR 1.5x (%)",
        "IRR 2.0x (%)",
    ]
    ws_sites.append(columns)
    for cell in ws_sites[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    fills = {
        "Green": PatternFill("solid", fgColor="DCFCE7"),
        "Amber": PatternFill("solid", fgColor="FEF3C7"),
        "Red": PatternFill("solid", fgColor="FEE2E2"),
    }

    for rank, s in enumerate(triage_result["sites"], start=1):
        row_vals = [
            rank,
            s["site_id"],
            s["status"],
            s["rank_score"],
            s["state"],
            s["input_lat"],
            s["input_lon"],
            s["opt_lat"],
            s["opt_lon"],
            s["meters_shifted"],
            s["elevation_m"],
            s["capex_saved_lakh"],
            s["typology"],
            s["statutory_gate"],
            s["bharatnet_ofc_m"],
            s["nearest_power_m"],
            s["solar_pv_kwp"],
            s["co2_offset_tpy"],
            s["beyond_tenancy_score"],
            s["total_capex_lakh"],
            s["monthly_opex_inr"],
            s["irr_1_0x_pct"],
            s["irr_1_5x_pct"],
            s["irr_2_0x_pct"],
        ]
        ws_sites.append(row_vals)
        status_cell = ws_sites.cell(row=rank + 1, column=3)
        status_cell.fill = fills.get(s["status"], fills["Amber"])
        status_cell.font = Font(bold=True)

    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    buf.seek(0)
    return buf.getvalue()


def _pdf_safe(text: object) -> str:
    s = str(text if text is not None else "-")
    s = s.replace("₹", "INR ").replace("₂", "2").replace("—", "-").replace("–", "-")
    return s.encode("latin-1", "replace").decode("latin-1")


def generate_dossier_pdf(report: dict, site_id: str = "SITE-001") -> bytes:
    """Generate a 1-page CFO/Bankable Feasibility PDF Dossier using fpdf2."""
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    # Header Banner
    pdf.set_fill_color(15, 23, 42)
    pdf.rect(0, 0, 210, 24, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_xy(10, 5)
    pdf.cell(140, 7, _pdf_safe(f"2030 TOWER FEASIBILITY DOSSIER | {site_id}"))
    pdf.set_font("Helvetica", "", 9)
    pdf.set_xy(10, 13)
    q = report["query"]
    sm = report["site_metrics"]
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"State: {sm.get('state')} | Soil: {sm.get('fao_soil')} | Query: ({q['lat']}, {q['lon']}) | 1km Pop: {sm.get('population_1km_catchment')}"
        ),
    )

    pdf.set_text_color(15, 23, 42)
    y = 28

    # Section 1: Search-Ring Auto-Optimizer
    ring = report["ring_optimizer"]
    deltas = ring["deltas"]
    opt = ring["recommended_pin"]
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_xy(10, y)
    pdf.cell(190, 6, "1. 500m SEARCH-RING AUTO-OPTIMIZER", ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_x(10)
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"Recommended Pin: ({opt['lat']}, {opt['lon']}) | Shifted: {deltas['meters_shifted']} m | Elev Gain: {deltas['elevation_gain_m']} m | Slope Reduction: {deltas['slope_reduction_pct']}% | Capex Saved: INR {deltas['capex_saved_lakh']} L"
        ),
        ln=1,
    )

    y = pdf.get_y() + 3
    # Section 2: 2030 Asset Typology, Zero-Diesel ESG & Beyond-Tenancy
    ent = report["enterprise_2030"]
    typ = ent["typology"]
    zd = ent["zero_diesel_esg"]
    bt = ent["beyond_tenancy"]
    stat = ent["statutory_row_gate"]

    pdf.set_font("Helvetica", "B", 10)
    pdf.set_xy(10, y)
    pdf.cell(190, 6, "2. 2030 ASSET TYPOLOGY, ZERO-DIESEL ESG & BEYOND-TENANCY", ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_x(10)
    pdf.cell(190, 5, _pdf_safe(f"Asset Typology: {typ['asset_typology']} - {typ['rationale']}"), ln=1)
    pdf.set_x(10)
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"Zero-Diesel: {zd['recommended_solar_pv_kwp']} kWp Solar + {zd['lifepo4_battery_kwh']} kWh LiFePO4 ({zd['lifepo4_battery_autonomy_hours']}h) | Diesel Saved: {zd['annual_diesel_saved_liters']} L/yr | CO2 Offset: {zd['co2_offset_tons_per_year']} t/yr"
        ),
        ln=1,
    )
    pdf.set_x(10)
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"Beyond-Tenancy: Micro-Edge Compute {bt['micro_edge_compute']['score']}/100 ({bt['micro_edge_compute']['tier']}) | EV Charging {bt['ev_charging_colocation']['score']}/100 ({bt['ev_charging_colocation']['tier']})"
        ),
        ln=1,
    )

    y = pdf.get_y() + 3
    # Section 3: Statutory & RoW Gate
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_xy(10, y)
    pdf.cell(190, 6, _pdf_safe(f"3. STATUTORY & RoW GATE ({stat['overall_gate_status']})"), ln=1)
    pdf.set_font("Helvetica", "", 8.5)
    gate_parts = []
    for gname, gval in stat["gates"].items():
        dist_txt = f"{gval['nearest_m']}m" if gval["nearest_m"] is not None else "Clear"
        gate_parts.append(f"{gname}: {gval['status']} ({dist_txt})")
    pdf.set_x(10)
    pdf.multi_cell(190, 4.5, _pdf_safe(" | ".join(gate_parts)))

    y = pdf.get_y() + 3
    # Section 4: Dual-Mode Backhaul & DEM LoS
    bh = report["backhaul_los"]
    fttt = bh["fiber_fttt"]
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_xy(10, y)
    pdf.cell(190, 6, _pdf_safe(f"4. DUAL-MODE BACKHAUL & DEM LoS ({bh['recommended_backhaul_mode']})"), ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_x(10)
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"BharatNet FTTT: Straight {fttt['nearest_bharatnet_straight_m']} m | Trench {fttt['estimated_trench_m']} m | Est. Cost: INR {fttt['estimated_fttt_capex_lakh']} L"
        ),
        ln=1,
    )
    for lnk in bh["microwave_los_links"][:3]:
        bv = lnk["band_verdicts"]
        pdf.set_x(10)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.cell(
            190,
            4.5,
            _pdf_safe(
                f"  -> Tower {lnk['target_tower_id']} ({lnk['operator']}, {lnk['distance_m']}m): LoS Clear={lnk['los_clear']} | 6GHz={bv['6_GHz']['status']} | 15GHz={bv['15_GHz']['status']} | 80GHz={bv['80_GHz_E_band']['status']}"
            ),
            ln=1,
        )

    y = pdf.get_y() + 3
    # Section 5: CFO Capex/Opex BOQ & Multi-Tenant IRR
    fin = report["financial_boq"]
    capex = fin["capex_boq_lakh"]
    opex = fin["opex_monthly_inr"]
    irr = fin["irr_sensitivity"]

    pdf.set_font("Helvetica", "B", 10)
    pdf.set_xy(10, y)
    pdf.cell(190, 6, "5. CFO CAPEX / OPEX BOQ & MULTI-TENANT IRR SENSITIVITY", ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_x(10)
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"Capex (INR L): Civil {capex['civil_foundation_lakh']} | Structure {capex['tower_structure_lakh']} | Solar/LiFePO4 {capex['solar_lifepo4_power_lakh']} | Backhaul {capex['backhaul_lakh']} => TOTAL: INR {capex['total_capex_lakh']} L"
        ),
        ln=1,
    )
    pdf.set_x(10)
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"Monthly Opex: INR {opex['total_monthly_opex_inr']:,}/mo (INR {opex['annual_opex_lakh']} L/yr)"
        ),
        ln=1,
    )

    # IRR Table
    y = pdf.get_y() + 2
    pdf.set_fill_color(241, 245, 249)
    pdf.set_font("Helvetica", "B", 8.5)
    headers = ["Tenancy", "Monthly Rev (INR)", "Monthly EBITDA (INR)", "10Y IRR (%)", "Payback (Yrs)"]
    widths = [30, 42, 44, 37, 37]
    pdf.set_x(10)
    for h, w in zip(headers, widths):
        pdf.cell(w, 6, h, border=1, fill=True, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 8.5)
    for key in ("1.0x", "1.5x", "2.0x"):
        row = irr[key]
        pdf.set_x(10)
        vals = [
            key,
            f"INR {row['monthly_revenue_inr']:,}",
            f"INR {row['monthly_ebitda_inr']:,}",
            f"{row['irr_10yr_pct']}%",
            f"{row['payback_years'] or '-'}",
        ]
        for v, w in zip(vals, widths):
            pdf.cell(w, 5.5, _pdf_safe(v), border=1, align="C")
        pdf.ln()

    # Footer
    pdf.set_xy(10, 282)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(190, 5, "Generated by 2030 Enterprise Tower Feasibility Platform | Spatial + DEM + Financial Engine")

    return bytes(pdf.output())


def generate_dossier_html(report: dict, site_id: str = "SITE-001") -> str:
    """Generate a standalone 1-page printable HTML CFO Feasibility Dossier."""
    q = report["query"]
    sm = report["site_metrics"]
    ring = report["ring_optimizer"]
    ent = report["enterprise_2030"]
    bh = report["backhaul_los"]
    fin = report["financial_boq"]
    irr = fin["irr_sensitivity"]

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Feasibility Dossier - {site_id}</title>
<style>
body {{ font-family: system-ui, -apple-system, sans-serif; margin: 24px; color: #0f172a; }}
.hdr {{ background: #0f172a; color: #fff; padding: 16px 20px; border-radius: 8px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 14px; }}
.card {{ border: 1px solid #cbd5e1; border-radius: 8px; padding: 12px; }}
h2 {{ font-size: 14px; margin: 0 0 8px; color: #1e293b; text-transform: uppercase; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }}
th, td {{ border: 1px solid #cbd5e1; padding: 6px 8px; text-align: center; }}
th {{ background: #f1f5f9; }}
</style></head><body>
<div class="hdr">
  <div style="font-size:18px;font-weight:700;">2030 Enterprise Tower Feasibility Dossier — {site_id}</div>
  <div style="font-size:12px;opacity:.85;">State: {sm.get('state')} | Soil: {sm.get('fao_soil')} | Query: ({q['lat']}, {q['lon']}) | 1km Pop: {sm.get('population_1km_catchment')}</div>
</div>
<div class="grid">
  <div class="card"><h2>Search-Ring Auto-Optimizer</h2>
    Recommended Pin: <b>({ring['recommended_pin']['lat']}, {ring['recommended_pin']['lon']})</b><br>
    Shifted: {ring['deltas']['meters_shifted']} m | Elev Gain: {ring['deltas']['elevation_gain_m']} m<br>
    Slope Reduction: {ring['deltas']['slope_reduction_pct']}% | Capex Saved: <b>₹{ring['deltas']['capex_saved_lakh']} L</b>
  </div>
  <div class="card"><h2>2030 Typology & Zero-Diesel ESG</h2>
    Typology: <b>{ent['typology']['asset_typology']}</b><br>
    Solar + LiFePO4: {ent['zero_diesel_esg']['recommended_solar_pv_kwp']} kWp + {ent['zero_diesel_esg']['lifepo4_battery_kwh']} kWh<br>
    CO₂ Offset: <b>{ent['zero_diesel_esg']['co2_offset_tons_per_year']} t/yr</b> | Statutory: <b>{ent['statutory_row_gate']['overall_gate_status']}</b>
  </div>
</div>
<div class="card" style="margin-top:12px;">
  <h2>CFO Capex/Opex BOQ (Total Capex: ₹{fin['capex_boq_lakh']['total_capex_lakh']} L | Monthly Opex: ₹{fin['opex_monthly_inr']['total_monthly_opex_inr']:,})</h2>
  <table>
    <tr><th>Tenancy</th><th>Monthly Rev</th><th>Monthly EBITDA</th><th>10Y IRR</th><th>Payback (Yrs)</th></tr>
    <tr><td>1.0x</td><td>₹{irr['1.0x']['monthly_revenue_inr']:,}</td><td>₹{irr['1.0x']['monthly_ebitda_inr']:,}</td><td>{irr['1.0x']['irr_10yr_pct']}%</td><td>{irr['1.0x']['payback_years']}</td></tr>
    <tr><td>1.5x</td><td>₹{irr['1.5x']['monthly_revenue_inr']:,}</td><td>₹{irr['1.5x']['monthly_ebitda_inr']:,}</td><td>{irr['1.5x']['irr_10yr_pct']}%</td><td>{irr['1.5x']['payback_years']}</td></tr>
    <tr><td>2.0x</td><td>₹{irr['2.0x']['monthly_revenue_inr']:,}</td><td>₹{irr['2.0x']['monthly_ebitda_inr']:,}</td><td>{irr['2.0x']['irr_10yr_pct']}%</td><td>{irr['2.0x']['payback_years']}</td></tr>
  </table>
</div>
</body></html>"""
