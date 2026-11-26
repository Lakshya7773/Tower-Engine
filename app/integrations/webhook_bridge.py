"""
Integration: Webhook Bridge — Sitetracker / ERPNext / n8n inbound & outbound dispatcher.

Inbound: parse webhook payload from partner system → run orchestrator → return report.
Outbound: async httpx POST with HMAC-SHA256 signature to callback_url.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import socket
from urllib.parse import urlparse
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.schemas.feasibility_v2 import (
    MasterReportV2,
    WebhookCallbackPayload,
    WebhookCallbackResponse,
    WebhookDispatchRequest,
    WebhookDispatchResponse,
    WebhookTargetSystem,
)

# ---------------------------------------------------------------------------
# Field mappings for each source system
# ---------------------------------------------------------------------------
_FIELD_MAPS: dict[str, dict[str, str]] = {
    "sitetracker": {
        # Sitetracker field → canonical
        "Site_Latitude__c": "lat",
        "Site_Longitude__c": "lon",
        "Site_Name__c": "site_id",
        "Search_Radius_m__c": "search_radius_m",
        "Latitude": "lat",
        "Longitude": "lon",
        "Name": "site_id",
    },
    "erpnext": {
        # ERPNext field → canonical
        "latitude": "lat",
        "longitude": "lon",
        "site_code": "site_id",
        "search_radius": "search_radius_m",
        "site_name": "site_id",
    },
    "n8n": {
        # n8n workflow payload → canonical (usually already normalized)
        "lat": "lat",
        "latitude": "lat",
        "lon": "lon",
        "lng": "lon",
        "longitude": "lon",
        "site_id": "site_id",
        "search_radius_m": "search_radius_m",
        "radius_m": "search_radius_m",
    },
}


def _extract_coords(
    payload: dict[str, Any],
    source: WebhookTargetSystem,
) -> dict[str, Any]:
    """Extract lat/lon/site_id/search_radius_m from raw partner payload."""
    field_map = _FIELD_MAPS.get(source, {})
    result: dict[str, Any] = {}

    for src_field, canon in field_map.items():
        val = payload.get(src_field)
        if val is not None and canon not in result:
            result[canon] = val

    # Fallback: direct canonical keys
    for canon in ("lat", "lon", "site_id", "search_radius_m"):
        if canon not in result and canon in payload:
            result[canon] = payload[canon]

    # Coerce types
    for num_field in ("lat", "lon", "search_radius_m"):
        if num_field in result:
            try:
                result[num_field] = float(str(result[num_field]).replace(",", "").strip())
            except (ValueError, TypeError):
                result.pop(num_field, None)

    return result


def _build_outbound_payload(
    report: MasterReportV2,
    target_system: WebhookTargetSystem,
    custom_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build the outbound JSON payload adapted for each target system."""
    fin = report.engines.financial_boq.data
    irr_15 = fin.irr_sensitivity.get("1.5x")

    base = {
        "site_id": report.site_id,
        "schema_version": "2.0",
        "verdict": report.verdict,
        "overall_score": report.overall_score,
        "typology": report.engines.typology_and_structural.data.asset_typology,
        "state": report.engines.spatial_base.data.site_metrics.state,
        "total_capex_lakh": fin.capex_boq_lakh.total_capex_lakh,
        "irr_1_5x_pct": irr_15.irr_10yr_pct if irr_15 else None,
        "co2_offset_tpy": report.engines.energy_esg.data.co2_offset_tons_per_year,
        "beyond_tenancy_score": report.engines.beyond_tenancy.data.composite_beyond_tenancy_score,
        "generated_at": report.generated_at,
    }
    base.update(custom_metadata)

    # System-specific field remapping
    if target_system == "sitetracker":
        return {
            "Site_Verdict__c": base["verdict"],
            "Overall_Score__c": base["overall_score"],
            "Asset_Typology__c": base["typology"],
            "Total_Capex_Lakh__c": base["total_capex_lakh"],
            "IRR_1_5x__c": base["irr_1_5x_pct"],
            "CO2_Offset_TPY__c": base["co2_offset_tpy"],
            "Generated_At__c": base["generated_at"],
            **{k: v for k, v in base.items() if k not in (
                "verdict", "overall_score", "typology", "total_capex_lakh",
                "irr_1_5x_pct", "co2_offset_tpy", "generated_at",
            )},
        }
    elif target_system == "erpnext":
        return {
            "doctype": "Tower Site Feasibility",
            "site_code": base["site_id"],
            "verdict": base["verdict"],
            "score": base["overall_score"],
            "asset_type": base["typology"],
            "capex_lakh": base["total_capex_lakh"],
            "irr_15x": base["irr_1_5x_pct"],
            "co2_offset": base["co2_offset_tpy"],
            **{k: v for k, v in custom_metadata.items()},
        }
    else:
        # n8n: return full base payload
        return base


def _hmac_signature(payload_bytes: bytes, secret: str) -> str:
    """HMAC-SHA256 hex signature for webhook payload verification."""
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()


def _validate_webhook_url(url: str) -> Optional[str]:
    """Validate outbound webhook URL against SSRF targets. Returns error string or None."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "Invalid webhook URL: only http/https with valid hostname allowed."
    if os.environ.get("ALLOW_PRIVATE_WEBHOOKS", "").strip().lower() in ("1", "true", "yes"):
        return None
    host = parsed.hostname
    if host.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return "Blocked webhook URL: loopback/localhost addresses are not permitted."
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return f"Blocked webhook URL: private/reserved IP ({ip}) not permitted."
    except socket.gaierror:
        # Unresolvable external host — let httpx handle connection failure
        pass
    except ValueError:
        return "Invalid webhook IP/hostname."
    return None


class WebhookBridge:
    """Synchronous webhook bridge (async httpx used only when available)."""

    def dispatch(self, req: WebhookDispatchRequest) -> WebhookDispatchResponse:
        """
        Dispatch a tower feasibility report to a partner webhook URL.

        1. Build the outbound payload
        2. Optionally sign with HMAC-SHA256
        3. POST synchronously (or async if event loop running)
        4. Return WebhookDispatchResponse
        """
        from app.services.orchestrator import run_full_assessment

        delivery_id = str(uuid.uuid4())
        dispatched_at = datetime.now(timezone.utc).isoformat()

        # Obtain the report (run orchestrator if not supplied)
        if req.report is not None:
            report = req.report
        else:
            report = run_full_assessment(
                lat=req.lat,  # type: ignore[arg-type]
                lon=req.lon,  # type: ignore[arg-type]
                search_radius_m=req.search_radius_m,
                site_id=req.site_id,
            )

        payload_dict = _build_outbound_payload(report, req.target_system, req.custom_metadata)
        payload_bytes = json.dumps(payload_dict, ensure_ascii=False, default=str).encode("utf-8")

        sig_header: Optional[str] = None
        if req.hmac_secret:
            sig_header = _hmac_signature(payload_bytes, req.hmac_secret)

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Delivery-ID": delivery_id,
            "X-Tower-Schema-Version": "2.0",
        }
        if req.auth_token:
            headers["Authorization"] = f"Bearer {req.auth_token}"
        if sig_header:
            headers["X-Hub-Signature-256"] = sig_header

        delivered = False
        http_status: Optional[int] = None
        response_body: Optional[str] = None
        error_msg: Optional[str] = _validate_webhook_url(req.webhook_url)

        if error_msg is None:
            try:
                import httpx
                with httpx.Client(timeout=15.0, follow_redirects=False) as client:
                    resp = client.post(req.webhook_url, content=payload_bytes, headers=headers)
                    http_status = resp.status_code
                    response_body = resp.text[:500] if resp.text else None
                    delivered = resp.status_code < 400
            except ImportError:
                error_msg = "httpx not installed — cannot dispatch webhook"
            except Exception as exc:
                error_msg = str(exc)[:200]

        return WebhookDispatchResponse(
            delivery_id=delivery_id,
            target_system=req.target_system,
            webhook_url=req.webhook_url,
            delivered=delivered,
            http_status=http_status,
            signature_header=sig_header,
            payload_sent=payload_dict,
            response_body=response_body,
            error=error_msg,
            dispatched_at=dispatched_at,
        )

    def receive_callback(
        self,
        raw_payload: dict[str, Any],
        raw_body: bytes,
        hmac_secret: Optional[str] = None,
        signature_header: Optional[str] = None,
    ) -> WebhookCallbackResponse:
        """Verify and record an inbound callback from a partner system."""
        verified = False
        if hmac_secret and signature_header:
            expected = _hmac_signature(raw_body, hmac_secret)
            verified = hmac.compare_digest(expected, signature_header)

        # Parse callback payload
        delivery_id = raw_payload.get("delivery_id", "UNKNOWN")
        target_sys: WebhookTargetSystem = raw_payload.get("target_system", "n8n")
        cb = WebhookCallbackPayload(
            delivery_id=delivery_id,
            target_system=target_sys,
            site_id=raw_payload.get("site_id", "UNKNOWN"),
            external_record_id=raw_payload.get("external_record_id"),
            status=raw_payload.get("status", "ACKNOWLEDGED"),
            notes=raw_payload.get("notes"),
            received_at=datetime.now(timezone.utc).isoformat(),
            metadata=raw_payload.get("metadata", {}),
        )

        return WebhookCallbackResponse(
            accepted=True,
            delivery_id=cb.delivery_id,
            verified_signature=verified,
            recorded_status=cb.status,
            processed_at=datetime.now(timezone.utc).isoformat(),
        )

    def parse_inbound(
        self,
        payload: dict[str, Any],
        source: WebhookTargetSystem,
    ) -> dict[str, Any]:
        """Parse an inbound webhook payload → canonical coordinates dict."""
        return _extract_coords(payload, source)
