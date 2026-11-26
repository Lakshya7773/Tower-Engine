"""
Integration: GeoJSON & Google Earth KML exporter.

Converts a MasterReportV2 or bare site coordinates into standard GeoJSON
FeatureCollection and KML Placemarks with no external geo dependencies.
Pure-stdlib xml.etree.ElementTree for KML generation.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any, Optional

from app.schemas.feasibility_v2 import (
    GeoJsonFeature,
    GeoJsonFeatureCollection,
    GeoJsonGeometry,
    MasterReportV2,
)


# ---------------------------------------------------------------------------
# GeoJSON exporter
# ---------------------------------------------------------------------------

def _site_props(report: MasterReportV2) -> dict[str, Any]:
    sb = report.engines.spatial_base.data
    ts = report.engines.typology_and_structural.data
    fin = report.engines.financial_boq.data
    ro = report.engines.ring_optimizer.data
    bt = report.engines.beyond_tenancy.data
    ee = report.engines.energy_esg.data
    return {
        "site_id": report.site_id,
        "verdict": report.verdict,
        "overall_score": report.overall_score,
        "typology": ts.asset_typology,
        "state": sb.site_metrics.state,
        "fao_soil": sb.site_metrics.fao_soil,
        "elevation_m": sb.site_metrics.dem_elevation_m,
        "total_capex_lakh": fin.capex_boq_lakh.total_capex_lakh,
        "irr_1_5x_pct": fin.irr_sensitivity.get("1.5x", {}).irr_10yr_pct
        if hasattr(fin.irr_sensitivity.get("1.5x", {}), "irr_10yr_pct") else None,
        "solar_kwp": ee.recommended_solar_pv_kwp,
        "co2_tpy": ee.co2_offset_tons_per_year,
        "beyond_tenancy_score": bt.composite_beyond_tenancy_score,
        "recommended_shift_m": ro.deltas.meters_shifted,
        "capex_saved_lakh": ro.deltas.capex_saved_lakh,
        "generated_at": report.generated_at,
    }


def to_geojson(
    report: Optional[MasterReportV2] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    site_id: str = "SITE-001",
    search_radius_m: float = 500.0,
    include_los_lines: bool = True,
    include_search_ring: bool = True,
) -> GeoJsonFeatureCollection:
    """Generate a GeoJSON FeatureCollection from a MasterReportV2 or bare coords."""

    features: list[GeoJsonFeature] = []

    if report is not None:
        _lat = report.input_lat
        _lon = report.input_lon
        _sid = report.site_id
        _radius = report.search_radius_m
        props = _site_props(report)
    else:
        _lat = lat  # type: ignore[assignment]
        _lon = lon  # type: ignore[assignment]
        _sid = site_id
        _radius = search_radius_m
        props = {"site_id": site_id}

    # Candidate pin
    features.append(GeoJsonFeature(
        id=f"{_sid}_input",
        geometry=GeoJsonGeometry(type="Point", coordinates=[_lon, _lat]),
        properties={**props, "feature_type": "input_pin"},
    ))

    # Recommended (optimised) pin
    if report is not None:
        ro = report.engines.ring_optimizer.data
        opt_lat = ro.recommended_pin.lat
        opt_lon = ro.recommended_pin.lon
        features.append(GeoJsonFeature(
            id=f"{_sid}_optimised",
            geometry=GeoJsonGeometry(type="Point", coordinates=[opt_lon, opt_lat]),
            properties={
                "site_id": _sid,
                "feature_type": "optimised_pin",
                "score": ro.recommended_pin.score,
                "meters_shifted": ro.deltas.meters_shifted,
                "capex_saved_lakh": ro.deltas.capex_saved_lakh,
            },
        ))

        # LoS lines to donor towers
        if include_los_lines:
            for lnk in report.engines.backhaul_los.data.microwave_los_links:
                features.append(GeoJsonFeature(
                    id=f"{_sid}_los_tower_{lnk.target_tower_id}",
                    geometry=GeoJsonGeometry(
                        type="LineString",
                        coordinates=[[_lon, _lat], [lnk.lon, lnk.lat]],
                    ),
                    properties={
                        "feature_type": "los_link",
                        "target_tower_id": lnk.target_tower_id,
                        "distance_m": lnk.distance_m,
                        "los_clear": lnk.los_clear,
                        "operator": lnk.operator,
                    },
                ))

        # Nearby towers as points
        for tower in report.engines.spatial_base.data.nearest_towers[:10]:
            features.append(GeoJsonFeature(
                id=f"tower_{tower.tower_id}",
                geometry=GeoJsonGeometry(type="Point", coordinates=[tower.lon, tower.lat]),
                properties={
                    "feature_type": "existing_tower",
                    "tower_id": tower.tower_id,
                    "operator": tower.operator,
                    "tech": tower.tech,
                    "distance_m": tower.distance_m,
                },
            ))

    # Search ring (approximate as polygon via circle of points)
    if include_search_ring:
        import math
        n_pts = 36
        ring_coords = []
        for i in range(n_pts + 1):
            angle = 2 * math.pi * i / n_pts
            dlat = (_radius / 111_320.0) * math.cos(angle)
            dlon = (_radius / (111_320.0 * math.cos(math.radians(_lat)))) * math.sin(angle)
            ring_coords.append([_lon + dlon, _lat + dlat])
        features.append(GeoJsonFeature(
            id=f"{_sid}_search_ring",
            geometry=GeoJsonGeometry(type="Polygon", coordinates=[ring_coords]),
            properties={
                "feature_type": "search_ring",
                "radius_m": _radius,
                "site_id": _sid,
            },
        ))

    return GeoJsonFeatureCollection(
        features=features,
        metadata={
            "site_id": _sid,
            "schema_version": "2.0",
            "crs": "EPSG:4326",
        },
    )


def to_geojson_str(fc: GeoJsonFeatureCollection) -> str:
    """Serialise GeoJsonFeatureCollection to a compact JSON string."""
    return json.dumps(fc.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# KML exporter (stdlib xml.etree.ElementTree — zero external dependencies)
# ---------------------------------------------------------------------------

def _kml_placemark(
    parent: ET.Element,
    name: str,
    description: str,
    lon: float,
    lat: float,
    elev: float = 0.0,
    style_url: str = "#pin",
    extended_data: Optional[dict[str, Any]] = None,
) -> None:
    pm = ET.SubElement(parent, "Placemark")
    ET.SubElement(pm, "name").text = name
    ET.SubElement(pm, "description").text = description
    ET.SubElement(pm, "styleUrl").text = style_url
    point = ET.SubElement(pm, "Point")
    ET.SubElement(point, "coordinates").text = f"{lon:.6f},{lat:.6f},{elev:.1f}"
    if extended_data:
        ed = ET.SubElement(pm, "ExtendedData")
        for k, v in extended_data.items():
            data_el = ET.SubElement(ed, "Data", name=str(k))
            ET.SubElement(data_el, "value").text = str(v)


def _kml_linestring(
    parent: ET.Element,
    name: str,
    coords: list[tuple[float, float]],
    description: str = "",
) -> None:
    pm = ET.SubElement(parent, "Placemark")
    ET.SubElement(pm, "name").text = name
    ET.SubElement(pm, "description").text = description
    ET.SubElement(pm, "styleUrl").text = "#losLine"
    ls = ET.SubElement(pm, "LineString")
    ET.SubElement(ls, "tessellate").text = "1"
    ET.SubElement(ls, "coordinates").text = " ".join(f"{ln:.6f},{lt:.6f},0" for lt, ln in coords)


def to_kml(
    report: Optional[MasterReportV2] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    site_id: str = "SITE-001",
    include_los_lines: bool = True,
) -> str:
    """Generate a KML string for Google Earth from a MasterReportV2 or bare coords."""

    ET.register_namespace("", "http://www.opengis.net/kml/2.2")
    kml = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    doc = ET.SubElement(kml, "Document")
    ET.SubElement(doc, "name").text = f"TowerFeasibility_{site_id}"

    # Styles
    for style_id, color in [("pin", "ff0000ff"), ("optPin", "ff00ff00"), ("tower", "ff00aaff")]:
        st = ET.SubElement(doc, "Style", id=style_id)
        icon_st = ET.SubElement(st, "IconStyle")
        ET.SubElement(icon_st, "color").text = color
        ET.SubElement(icon_st, "scale").text = "1.2"
        icon = ET.SubElement(icon_st, "Icon")
        ET.SubElement(icon, "href").text = "http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png"

    los_st = ET.SubElement(doc, "Style", id="losLine")
    line_st = ET.SubElement(los_st, "LineStyle")
    ET.SubElement(line_st, "color").text = "ff0088ff"
    ET.SubElement(line_st, "width").text = "2"

    _lat = report.input_lat if report else lat
    _lon = report.input_lon if report else lon
    _sid = report.site_id if report else site_id

    # Candidate input pin
    _kml_placemark(
        doc, f"Input Pin — {_sid}",
        f"Candidate tower site: {_sid}",
        _lon, _lat, style_url="#pin",  # type: ignore[arg-type]
        extended_data={"site_id": _sid, "lat": _lat, "lon": _lon},
    )

    if report is not None:
        ro = report.engines.ring_optimizer.data
        # Optimised pin
        _kml_placemark(
            doc, f"Optimised Pin — {_sid}",
            f"Shifted {ro.deltas.meters_shifted:.0f}m → ₹{ro.deltas.capex_saved_lakh:.2f}L saved",
            ro.recommended_pin.lon, ro.recommended_pin.lat, style_url="#optPin",
            extended_data={
                "meters_shifted": ro.deltas.meters_shifted,
                "capex_saved_lakh": ro.deltas.capex_saved_lakh,
                "score": ro.recommended_pin.score,
            },
        )

        # Existing nearby towers
        for tower in report.engines.spatial_base.data.nearest_towers[:10]:
            _kml_placemark(
                doc, f"Tower {tower.tower_id} ({tower.operator or 'Unknown'})",
                f"Tech: {tower.tech or 'N/A'} | Dist: {tower.distance_m:.0f}m",
                tower.lon, tower.lat, style_url="#tower",
                extended_data={"tower_id": tower.tower_id, "operator": tower.operator, "distance_m": tower.distance_m},
            )

        # LoS lines
        if include_los_lines:
            for lnk in report.engines.backhaul_los.data.microwave_los_links:
                _kml_linestring(
                    doc,
                    f"LoS → Tower {lnk.target_tower_id} ({'CLEAR' if lnk.los_clear else 'BLOCKED'})",
                    [(_lat, _lon), (lnk.lat, lnk.lon)],  # type: ignore[arg-type]
                    description=f"Distance: {lnk.distance_m:.0f}m | Mode: {report.engines.backhaul_los.data.recommended_backhaul_mode}",
                )

    tree = ET.ElementTree(kml)
    import io
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue().decode("utf-8")
