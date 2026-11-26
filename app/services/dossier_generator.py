"""
Bankable Dossier Generator (v2) — 1-page CFO & Engineering Feasibility Dossier (HTML & PDF).

Prominently displays the 3 Hero Callout Cards at the top, followed by Evidence Audit
provenance, Fresnel LoS clearance, Solar/ESG savings, GatiShakti RoW status, Surgical SOA
Checklist, and the Capex/Opex Multi-Tenant IRR table.
"""

from __future__ import annotations

import html
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.schemas.feasibility_v2 import MasterReportV2


def _pdf_safe(text: object) -> str:
    """Sanitize Unicode characters unsupported by built-in Helvetica Latin-1 font."""
    s = str(text)
    replacements = {
        "₹": "Rs.",
        "—": "-",
        "–": "-",
        "→": "->",
        "₂": "2",
        "≥": ">=",
        "≤": "<=",
        "×": "x",
        "±": "+/-",
        "√": "sqrt",
        "📍": "[PIN]",
        "📡": "[LOS]",
        "🏗️": "[2030]",
        "•": "*",
        "“": '"',
        "”": '"',
        "’": "'",
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def generate_dossier_html_v2(report: MasterReportV2) -> str:
    """Render a self-contained printable 1-page HTML Bankable Feasibility Dossier."""
    sb = report.engines.spatial_base.data
    ro = report.engines.ring_optimizer.data
    bl = report.engines.backhaul_los.data
    ts = report.engines.typology_and_structural.data
    esg = report.engines.energy_esg.data
    sr = report.engines.statutory_row.data
    bt = report.engines.beyond_tenancy.data
    fin = report.engines.financial_boq.data
    soa = report.engines.soa_checklist.data

    verdict_color = {
        "GO": "#166534",
        "CONDITIONAL_GO": "#b45309",
        "NO_GO": "#991b1b",
    }.get(report.verdict, "#1e3a5f")

    hero_html = ""
    for card in report.hero_cards:
        badge_bg = "#dcfce7" if card.badge == "PASS" else ("#fef9c3" if card.badge == "CONDITIONAL" else "#e0f2fe")
        badge_fg = "#166534" if card.badge == "PASS" else ("#92400e" if card.badge == "CONDITIONAL" else "#075985")
        hero_html += f"""
        <div class="hero-card">
          <div class="hero-head">
            <span class="hero-title">{html.escape(card.title)}</span>
            <span class="badge" style="background:{badge_bg};color:{badge_fg};">{html.escape(card.badge)}</span>
          </div>
          <div class="hero-metric">{html.escape(card.headline_metric)}</div>
          <div class="hero-sub">{html.escape(card.subheadline)}</div>
          <div class="hero-ev">Evidence: <code>{html.escape(card.evidence_ref)}</code></div>
        </div>
        """

    # IRR rows
    irr_rows_html = ""
    for label, sc in fin.irr_sensitivity.items():
        irr_val = f"{sc.irr_10yr_pct:.2f}%" if sc.irr_10yr_pct is not None else "N/A"
        pb_val = f"{sc.payback_years:.1f} yr" if sc.payback_years is not None else "N/A"
        npv_val = f"Rs.{sc.npv_lakh:.2f}L" if sc.npv_lakh is not None else "N/A"
        irr_rows_html += f"""
        <tr>
          <td><strong>{html.escape(label)}</strong></td>
          <td>₹{sc.monthly_revenue_inr:,}</td>
          <td>₹{sc.monthly_ebitda_inr:,}</td>
          <td><strong>{irr_val}</strong></td>
          <td>{npv_val}</td>
          <td>{pb_val}</td>
        </tr>
        """

    # LoS links rows
    los_rows_html = ""
    for lnk in bl.microwave_los_links[:3]:
        b15 = lnk.band_verdicts.get("15_GHz")
        b80 = lnk.band_verdicts.get("80_GHz_E_band")
        los_rows_html += f"""
        <tr>
          <td>Tower #{lnk.target_tower_id} ({html.escape(lnk.operator or 'N/A')})</td>
          <td>{lnk.distance_m:.0f} m</td>
          <td>{lnk.min_terrain_clearance_m:.1f} m</td>
          <td>{'CLEAR' if lnk.los_clear else 'BLOCKED'}</td>
          <td>{html.escape(b15.status if b15 else 'N/A')}</td>
          <td>{html.escape(b80.status if b80 else 'N/A')}</td>
        </tr>
        """
    if not los_rows_html:
        los_rows_html = '<tr><td colspan="6">No donor towers within LoS search radius.</td></tr>'

    # Statutory gates rows
    gate_rows_html = ""
    for cat, g in sr.gates.items():
        dist_s = f"{g.nearest_m:.0f} m" if g.nearest_m is not None else "> warn radius"
        gate_rows_html += f"""
        <tr>
          <td>{html.escape(cat)}</td>
          <td><strong>{html.escape(g.status)}</strong></td>
          <td>{dist_s}</td>
          <td>{html.escape(g.authority)}</td>
        </tr>
        """

    # SOA Checklist
    soa_html = ""
    for item in soa.items:
        soa_html += f"""
        <li>
          <span class="prio prio-{html.escape(item.priority)}">{html.escape(item.priority)}</span>
          <strong>{html.escape(item.action_title)}</strong> — {html.escape(item.trigger_evidence)}
          <em>({html.escape(item.owner_role)})</em>
        </li>
        """

    # Evidence audit summary
    engine_list = [
        report.engines.spatial_base,
        report.engines.ring_optimizer,
        report.engines.backhaul_los,
        report.engines.typology_and_structural,
        report.engines.energy_esg,
        report.engines.statutory_row,
        report.engines.beyond_tenancy,
        report.engines.financial_boq,
        report.engines.soa_checklist,
    ]
    ev_rows_html = ""
    for eng in engine_list:
        ev = eng.evidence_audit
        ev_rows_html += f"""
        <tr>
          <td>{html.escape(eng.engine_id)}</td>
          <td>{eng.score:.1f}</td>
          <td>{html.escape(eng.status)}</td>
          <td><code>{html.escape(ev.data_source)}</code></td>
          <td>{html.escape(ev.confidence)}</td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Bankable Feasibility Dossier — {html.escape(report.site_id)}</title>
<style>
  @page {{ size: A4; margin: 10mm; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #0f172a; margin: 0; padding: 16px; background: #f8fafc; font-size: 12px; }}
  .page {{ max-width: 980px; margin: 0 auto; background: #fff; padding: 20px 24px; border: 1px solid #e2e8f0; border-radius: 8px; }}
  .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #0f172a; padding-bottom: 10px; margin-bottom: 14px; }}
  .header h1 {{ margin: 0; font-size: 18px; color: #0f172a; }}
  .verdict-pill {{ padding: 6px 14px; border-radius: 999px; color: #fff; font-weight: 700; font-size: 13px; background: {verdict_color}; }}
  .meta {{ color: #475569; font-size: 11px; margin-top: 4px; }}
  .hero-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 14px; }}
  .hero-card {{ border: 1px solid #cbd5e1; border-top: 3px solid #0284c7; border-radius: 6px; padding: 10px; background: #f8fafc; }}
  .hero-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }}
  .hero-title {{ font-weight: 700; font-size: 11px; color: #334155; }}
  .badge {{ font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; }}
  .hero-metric {{ font-size: 16px; font-weight: 800; color: #0f172a; margin: 4px 0; }}
  .hero-sub {{ font-size: 11px; color: #475569; line-height: 1.3; }}
  .hero-ev {{ font-size: 10px; color: #64748b; margin-top: 6px; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }}
  h2 {{ font-size: 13px; color: #1e3a5f; border-bottom: 1px solid #e2e8f0; padding-bottom: 3px; margin: 10px 0 6px; text-transform: uppercase; letter-spacing: 0.4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 11px; margin-bottom: 8px; }}
  th, td {{ border: 1px solid #e2e8f0; padding: 4px 6px; text-align: left; }}
  th {{ background: #f1f5f9; font-weight: 600; color: #334155; }}
  ul {{ margin: 4px 0; padding-left: 18px; }}
  li {{ margin-bottom: 4px; line-height: 1.35; }}
  .prio {{ font-size: 9px; font-weight: 700; padding: 1px 5px; border-radius: 3px; margin-right: 4px; }}
  .prio-CRITICAL {{ background: #fee2e2; color: #991b1b; }}
  .prio-HIGH {{ background: #fef3c7; color: #92400e; }}
  .prio-MEDIUM {{ background: #e0f2fe; color: #075985; }}
  .print-btn {{ float: right; padding: 6px 12px; background: #0f172a; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 11px; }}
  @media print {{ .print-btn {{ display: none; }} body {{ background: #fff; padding: 0; }} .page {{ border: none; padding: 0; }} }}
</style>
</head>
<body>
<div class="page">
  <button class="print-btn" onclick="window.print()">Print / Save PDF</button>
  <div class="header">
    <div>
      <h1>2030 Enterprise Tower Feasibility — Bankable CFO &amp; Engineering Dossier</h1>
      <div class="meta">
        Site ID: <strong>{html.escape(report.site_id)}</strong> |
        Input: ({report.input_lat:.5f}, {report.input_lon:.5f}) |
        Optimal Pin: ({ro.recommended_pin.lat:.5f}, {ro.recommended_pin.lon:.5f}) |
        State: <strong>{html.escape(sb.site_metrics.state)}</strong> |
        Typology: <strong>{html.escape(ts.asset_typology)}</strong> |
        Generated: {html.escape(report.generated_at)}
      </div>
    </div>
    <div class="verdict-pill">{html.escape(report.verdict)} ({report.overall_score:.1f}/100)</div>
  </div>

  <div class="hero-grid">{hero_html}</div>

  <div class="grid-2">
    <div>
      <h2>Capex BOQ &amp; Monthly Opex</h2>
      <table>
        <tr><th>Component</th><th>Amount</th></tr>
        <tr><td>Civil &amp; Foundation ({html.escape(ts.soil_foundation.fao_soil_class)}, Wind Zone {ts.wind_zone_spec.wind_zone})</td><td>₹{fin.capex_boq_lakh.civil_foundation_lakh:.2f} L</td></tr>
        <tr><td>Tower Structure ({ts.wind_zone_spec.mast_height_m:.0f}m, {ts.wind_zone_spec.basic_wind_speed_ms:.0f} m/s)</td><td>₹{fin.capex_boq_lakh.tower_structure_lakh:.2f} L</td></tr>
        <tr><td>Solar ({esg.recommended_solar_pv_kwp:.1f} kWp) + LiFePO4 ({esg.lifepo4_battery_kwh:.0f} kWh)</td><td>₹{fin.capex_boq_lakh.solar_lifepo4_power_lakh:.2f} L</td></tr>
        <tr><td>Backhaul ({html.escape(bl.recommended_backhaul_mode)})</td><td>₹{fin.capex_boq_lakh.backhaul_lakh:.2f} L</td></tr>
        <tr><td>Edge / EV Co-Location Add-On</td><td>₹{fin.capex_boq_lakh.edge_ev_addon_capex_lakh:.2f} L</td></tr>
        <tr><th>Total Capex</th><th>₹{fin.capex_boq_lakh.total_capex_lakh:.2f} L</th></tr>
        <tr><td>Total Monthly Opex</td><td>₹{fin.opex_monthly_inr.total_monthly_opex_inr:,}/mo</td></tr>
      </table>

      <h2>Multi-Tenant IRR Sensitivity (10-Yr)</h2>
      <table>
        <tr><th>Scenario</th><th>Rev/mo</th><th>EBITDA/mo</th><th>IRR</th><th>NPV</th><th>Payback</th></tr>
        {irr_rows_html}
      </table>
    </div>

    <div>
      <h2>Zero-Diesel ESG &amp; Beyond-Tenancy</h2>
      <table>
        <tr><th>Metric</th><th>Value</th></tr>
        <tr><td>Solar PVOUT / Array</td><td>{esg.solar_pvout_kwh_kwp_day:.2f} kWh/kWp/d &rarr; {esg.recommended_solar_pv_kwp:.1f} kWp</td></tr>
        <tr><td>LiFePO4 Autonomy</td><td>{esg.lifepo4_battery_autonomy_hours}h ({esg.lifepo4_battery_kwh:.0f} kWh)</td></tr>
        <tr><td>Annual Diesel Saved</td><td>{esg.annual_diesel_saved_liters:,} L/yr (₹{esg.annual_diesel_cost_saved_inr:,}/yr)</td></tr>
        <tr><td>Scope 1 CO<sub>2</sub> Offset</td><td><strong>{esg.co2_offset_tons_per_year:.2f} tCO<sub>2</sub>/yr</strong> ({esg.ten_year_co2_offset_tons:.1f} t / 10 yr)</td></tr>
        <tr><td>Micro-Edge / EV Co-Location</td><td>Edge {bt.micro_edge_compute.score}/100, EV {bt.ev_charging_colocation.score}/100 (₹{bt.estimated_total_ancillary_monthly_inr:,}/mo)</td></tr>
      </table>

      <h2>GatiShakti Statutory &amp; RoW Gate ({html.escape(sr.overall_gate_status)})</h2>
      <table>
        <tr><th>Category</th><th>Status</th><th>Nearest</th><th>Authority</th></tr>
        {gate_rows_html}
      </table>
    </div>
  </div>

  <h2>Dual-Mode Backhaul &amp; 60% Fresnel LoS Clearance</h2>
  <table>
    <tr><th>Donor Tower</th><th>Distance</th><th>Min Clearance</th><th>LoS Verdict</th><th>15 GHz Band</th><th>80 GHz E-Band</th></tr>
    {los_rows_html}
  </table>

  <div class="grid-2">
    <div>
      <h2>Surgical SOA Field Verification Checklist ({soa.estimated_field_hours:.0f}h Est.)</h2>
      <ul>{soa_html}</ul>
    </div>
    <div>
      <h2>Evidence Audit Provenance ("Show Your Math")</h2>
      <table>
        <tr><th>Engine</th><th>Score</th><th>Status</th><th>Data Source</th><th>Confidence</th></tr>
        {ev_rows_html}
      </table>
    </div>
  </div>
</div>
</body>
</html>"""


def generate_dossier_pdf_v2(report: MasterReportV2) -> bytes:
    """Generate a 1–2 page bankable PDF dossier using fpdf2 without deprecation warnings."""
    sb = report.engines.spatial_base.data
    ro = report.engines.ring_optimizer.data
    bl = report.engines.backhaul_los.data
    ts = report.engines.typology_and_structural.data
    esg = report.engines.energy_esg.data
    sr = report.engines.statutory_row.data
    bt = report.engines.beyond_tenancy.data
    fin = report.engines.financial_boq.data
    soa = report.engines.soa_checklist.data

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=10)
    pdf.add_page()

    # Header
    pdf.set_fill_color(15, 23, 42)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(
        190,
        9,
        _pdf_safe(f"2030 BANKABLE TOWER FEASIBILITY DOSSIER | {report.site_id} | {report.verdict} ({report.overall_score:.1f}/100)"),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
        fill=True,
    )
    pdf.set_text_color(30, 41, 59)
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"State: {sb.site_metrics.state} | Typology: {ts.asset_typology} | "
            f"Input: ({report.input_lat:.5f}, {report.input_lon:.5f}) -> Opt: ({ro.recommended_pin.lat:.5f}, {ro.recommended_pin.lon:.5f}) | "
            f"Wind Zone {ts.wind_zone_spec.wind_zone} ({ts.wind_zone_spec.basic_wind_speed_ms:.0f} m/s)"
        ),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.ln(2)

    # 3 Hero Callout Cards
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(224, 242, 254)
    pdf.cell(190, 6, "EXECUTIVE HERO CALLOUT CARDS", new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True)
    pdf.set_font("Helvetica", "", 8)
    for card in report.hero_cards:
        pdf.cell(
            190,
            5,
            _pdf_safe(f"[{card.badge}] {card.title}: {card.headline_metric} | {card.subheadline} (Ref: {card.evidence_ref})"),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
    pdf.ln(2)

    # Section 1: Fresnel LoS & Backhaul
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(241, 245, 249)
    pdf.cell(
        190,
        6,
        _pdf_safe(f"1. BACKHAUL & 60% FRESNEL LoS ({bl.recommended_backhaul_mode})"),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
        fill=True,
    )
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"BharatNet OFC: {bl.fiber_fttt.nearest_bharatnet_straight_m or 'N/A'} m | "
            f"FTTT Capex: {bl.fiber_fttt.estimated_fttt_capex_lakh or 'N/A'} Rs.L | "
            f"Donor Links Evaluated: {len(bl.microwave_los_links)}"
        ),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    for lnk in bl.microwave_los_links[:3]:
        b15 = lnk.band_verdicts.get("15_GHz")
        pdf.cell(
            190,
            4.5,
            _pdf_safe(
                f"  - Tower #{lnk.target_tower_id} ({lnk.distance_m:.0f}m): "
                f"{'CLEAR' if lnk.los_clear else 'BLOCKED'}, min clearance {lnk.min_terrain_clearance_m:.1f}m, "
                f"15GHz={b15.status if b15 else 'N/A'}"
            ),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
    pdf.ln(1.5)

    # Section 2: Zero-Diesel ESG & GatiShakti RoW
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(
        190,
        6,
        _pdf_safe(f"2. SOLAR/ESG, BEYOND-TENANCY & GATISHAKTI RoW ({sr.overall_gate_status})"),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
        fill=True,
    )
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"Solar: {esg.recommended_solar_pv_kwp:.1f} kWp + LiFePO4 {esg.lifepo4_battery_kwh:.0f} kWh ({esg.lifepo4_battery_autonomy_hours}h) | "
            f"Diesel Saved: {esg.annual_diesel_saved_liters:,} L/yr | CO2 Offset: {esg.co2_offset_tons_per_year:.2f} tCO2/yr"
        ),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"Beyond-Tenancy: Edge={bt.micro_edge_compute.score}/100, EV={bt.ev_charging_colocation.score}/100, "
            f"Ancillary=Rs.{bt.estimated_total_ancillary_monthly_inr:,}/mo | "
            f"GatiShakti Eligible: {'YES' if sr.gatishakti_row_eligible else 'NO'}"
        ),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.ln(1.5)

    # Section 3: Capex/Opex & Multi-Tenant IRR Table
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(190, 6, "3. CFO CAPEX / OPEX BOQ & MULTI-TENANT IRR SENSITIVITY", new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True)
    pdf.set_font("Helvetica", "", 8)
    boq = fin.capex_boq_lakh
    pdf.cell(
        190,
        5,
        _pdf_safe(
            f"Capex: Civil Rs.{boq.civil_foundation_lakh:.2f}L + Struct Rs.{boq.tower_structure_lakh:.2f}L + "
            f"Power Rs.{boq.solar_lifepo4_power_lakh:.2f}L + BH Rs.{boq.backhaul_lakh:.2f}L + "
            f"Edge/EV Rs.{boq.edge_ev_addon_capex_lakh:.2f}L = TOTAL Rs.{boq.total_capex_lakh:.2f}L | "
            f"Opex: Rs.{fin.opex_monthly_inr.total_monthly_opex_inr:,}/mo"
        ),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    for label, sc in fin.irr_sensitivity.items():
        irr_s = f"{sc.irr_10yr_pct:.2f}%" if sc.irr_10yr_pct is not None else "N/A"
        pb_s = f"{sc.payback_years:.1f}y" if sc.payback_years is not None else "N/A"
        pdf.cell(
            190,
            4.5,
            _pdf_safe(
                f"  - {label}: Rev Rs.{sc.monthly_revenue_inr:,}/mo | EBITDA Rs.{sc.monthly_ebitda_inr:,}/mo | "
                f"10yr IRR {irr_s} | Payback {pb_s}"
            ),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
    pdf.ln(1.5)

    # Section 4: Surgical SOA Checklist & Evidence Audit
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(
        190,
        6,
        _pdf_safe(f"4. SURGICAL SOA FIELD CHECKLIST ({soa.critical_count} Critical, {soa.estimated_field_hours:.0f}h Est.)"),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
        fill=True,
    )
    pdf.set_font("Helvetica", "", 8)
    for item in soa.items:
        pdf.cell(
            190,
            4.5,
            _pdf_safe(f"  [{item.priority}] {item.action_title} - {item.trigger_evidence}"),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
    pdf.ln(1.5)

    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(190, 6, "5. EVIDENCE AUDIT PROVENANCE", new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True)
    pdf.set_font("Helvetica", "", 7.5)
    for eng in (
        report.engines.spatial_base,
        report.engines.ring_optimizer,
        report.engines.backhaul_los,
        report.engines.typology_and_structural,
        report.engines.energy_esg,
        report.engines.statutory_row,
        report.engines.financial_boq,
    ):
        pdf.cell(
            190,
            4,
            _pdf_safe(f"  * {eng.engine_id}: score={eng.score:.1f} ({eng.status}) | source={eng.evidence_audit.data_source} [{eng.evidence_audit.confidence}]"),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )

    out = pdf.output()
    return bytes(out) if not isinstance(out, bytes) else out
