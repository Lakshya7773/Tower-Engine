"""Strict Pydantic v2 schemas and interface contracts for Tower Feasibility Engine v2."""

from __future__ import annotations

from typing import Any, Generic, Literal, Optional, TypeVar

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

ConfidenceLevel = Literal["HIGH", "MEDIUM", "LOW"]
EngineStatus = Literal["PASS", "CONDITIONAL", "HOLD", "INFO"]
OverallVerdict = Literal["GO", "CONDITIONAL_GO", "NO_GO"]
AssetTypology = Literal[
    "Macro GBT",
    "Urban RTT/Monopole",
    "5G Street-Furniture Small Cell",
    "Industrial Private 5G / Edge Node",
]
HeroCardId = Literal[
    "pin_shift_capex_arbitrage",
    "virtual_los_verdict",
    "colocation_edge_ev_arbitrage",
]
WebhookTargetSystem = Literal["sitetracker", "erpnext", "n8n"]

HERO_CARD_ORDER: tuple[HeroCardId, HeroCardId, HeroCardId] = (
    "pin_shift_capex_arbitrage",
    "virtual_los_verdict",
    "colocation_edge_ev_arbitrage",
)

T = TypeVar("T")


class StrictSchema(BaseModel):
    """Base model forbidding undeclared fields while supporting field-name & alias population."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EvidenceAudit(StrictSchema):
    """Mandatory evidence and provenance block attached to every v2 engine result."""

    data_source: str = Field(..., min_length=1)
    confidence: ConfidenceLevel
    raw_measurements: dict[str, Any] = Field(default_factory=dict)
    coordinates: dict[str, Any] = Field(default_factory=dict)
    distances_m: dict[str, float] = Field(default_factory=dict)
    formulas: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class EngineResult(StrictSchema, Generic[T]):
    """Standardized return envelope for all 9 feasibility engines."""

    engine_id: str = Field(..., min_length=1)
    engine_name: str = Field(..., min_length=1)
    score: float = Field(..., ge=0.0, le=100.0)
    status: EngineStatus
    summary: str = Field(..., min_length=1)
    evidence_audit: EvidenceAudit
    data: T


class HeroCard(StrictSchema):
    """Executive decision card surfaced on MasterReportV2, PDF/HTML dossiers, and Cockpit UI."""

    card_id: HeroCardId
    title: str = Field(..., min_length=1)
    headline_metric: str = Field(..., min_length=1)
    subheadline: str = Field(..., min_length=1)
    badge: str = Field(..., min_length=1)
    evidence_ref: str = Field(..., min_length=1)
    metrics: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Engine 1: Spatial Base
# ---------------------------------------------------------------------------
class SpatialQueryMeta(StrictSchema):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    radius_km: float = Field(..., gt=0.0)
    search_radius_m: float = Field(500.0, gt=0.0)
    effective_tower_lookup_km: float = Field(5.0, gt=0.0)


class SiteMetrics(StrictSchema):
    state: str = "UNKNOWN"
    fao_soil: str = "UNKNOWN"
    dem_elevation_m: Optional[float] = None
    solar_pvout_kwh_kwp: Optional[float] = None
    population_1km_catchment: Optional[int] = None


class InfrastructureMetrics(StrictSchema):
    towers_in_radius: int = 0
    nearest_tower_m: Optional[float] = None
    nearest_power_substation_m: Optional[float] = None
    bharatnet_segments_in_bbox: int = 0
    nearest_bharatnet_ofc_m: Optional[float] = None
    exclusion_zones_in_radius: int = 0
    exclusion_zones_in_context_radius: int = 0
    nearest_exclusion_m: Optional[float] = None


class NearbyTower(StrictSchema):
    tower_id: int
    primary_source: Optional[str] = None
    verified_sources: Optional[str] = None
    operator: Optional[str] = None
    tech: Optional[str] = None
    state_name: Optional[str] = None
    fao_soil: Optional[str] = None
    nearest_bbnl_ofc_m: Optional[float] = None
    lat: float
    lon: float
    distance_m: float


class NearbyExclusion(StrictSchema):
    excl_id: int
    name: str = "UNNAMED"
    other_tags: Optional[str] = None
    distance_m: float
    lat: Optional[float] = None
    lon: Optional[float] = None


class SchemaInspectionSummary(StrictSchema):
    db_path: str
    db_exists: bool
    schema_verified: bool
    sqlite_source: str
    tables_verified: list[str] = Field(default_factory=list)
    columns_by_table: dict[str, list[str]] = Field(default_factory=dict)
    missing_tables: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class SpatialBaseData(StrictSchema):
    query: SpatialQueryMeta
    site_metrics: SiteMetrics
    infrastructure: InfrastructureMetrics
    nearest_towers: list[NearbyTower] = Field(default_factory=list)
    nearest_exclusions: list[NearbyExclusion] = Field(default_factory=list)
    towers_in_search_radius: int = 0
    tower_fallback_radius_m: Optional[float] = None
    fallback_used: bool = False
    state_source: str = ""
    fao_soil_source: str = ""
    raster_status: dict[str, Any] = Field(default_factory=dict)
    schema_summary: Optional[SchemaInspectionSummary] = None

    @property
    def state(self) -> str:
        return self.site_metrics.state

    @property
    def fao_soil(self) -> str:
        return self.site_metrics.fao_soil


SpatialBaseResult = EngineResult[SpatialBaseData]


# ---------------------------------------------------------------------------
# Engine 2: Search-Ring 25-Point Grid Optimizer
# ---------------------------------------------------------------------------
class RingPinBaseline(StrictSchema):
    lat: float
    lon: float
    elevation_m: Optional[float] = None
    slope_pct: float
    bharatnet_ofc_m: Optional[float] = None


class RingPinRecommended(StrictSchema):
    lat: float
    lon: float
    elevation_m: Optional[float] = None
    slope_pct: float
    bharatnet_ofc_m: Optional[float] = None
    nearest_exclusion_m: Optional[float] = None
    score: float


class RingOptimizationDeltas(StrictSchema):
    meters_shifted: float
    elevation_gain_m: float
    slope_reduction_pct: float
    bharatnet_delta_m: float
    capex_saved_lakh: float
    soil_class: str
    soil_risk_multiplier: float


class RingGridCandidate(StrictSchema):
    lat: float
    lon: float
    is_origin: bool
    elevation_m: Optional[float] = None
    slope_pct: float
    bharatnet_ofc_m: Optional[float] = None
    nearest_exclusion_m: Optional[float] = None
    score: float


class RingOptimizerData(StrictSchema):
    input_pin: RingPinBaseline
    recommended_pin: RingPinRecommended
    deltas: RingOptimizationDeltas
    search_radius_m: float
    candidates_evaluated: int = Field(..., ge=1, le=25)
    grid_points: list[RingGridCandidate] = Field(default_factory=list)


RingOptimizerResult = EngineResult[RingOptimizerData]


# ---------------------------------------------------------------------------
# Engine 3: Backhaul & 60% Fresnel LoS Ray-Tracer
# ---------------------------------------------------------------------------
class FiberFtttSummary(StrictSchema):
    nearest_bharatnet_straight_m: Optional[float] = None
    estimated_trench_m: Optional[float] = None
    estimated_fttt_capex_lakh: Optional[float] = None
    fiber_primary_recommended: bool


class FresnelBandVerdict(StrictSchema):
    frequency_ghz: float
    status: Literal[
        "CLEAR_60PCT_FRESNEL",
        "MARGINAL_PARTIAL_FRESNEL",
        "BLOCKED",
        "RAIN_FADE_RANGE_EXCEEDED",
    ]
    min_fresnel_clearance_pct: float
    viable: bool


class LosProfilePoint(StrictSchema):
    step: int
    distance_m: float
    ground_elev_m: float
    earth_bulge_m: float
    effective_terrain_m: float
    los_line_m: float
    fresnel_60_bottom_15ghz_m: float
    clearance_m: float


class MicrowaveLosLink(StrictSchema):
    target_tower_id: int
    operator: Optional[str] = None
    tech: Optional[str] = None
    lat: float
    lon: float
    distance_m: float
    site_elev_m: float
    tower_elev_m: float
    min_terrain_clearance_m: float
    los_clear: bool
    band_verdicts: dict[str, FresnelBandVerdict]
    elevation_profile: list[LosProfilePoint]


class BackhaulLosData(StrictSchema):
    fiber_fttt: FiberFtttSummary
    microwave_los_links: list[MicrowaveLosLink] = Field(default_factory=list)
    recommended_backhaul_mode: str
    primary_los_profile: list[LosProfilePoint] = Field(default_factory=list)


BackhaulLosResult = EngineResult[BackhaulLosData]


# ---------------------------------------------------------------------------
# Engine 4: 2030 Asset Typology + IS 875 Wind Zone & Soil Foundation Engine
# ---------------------------------------------------------------------------
class WindZoneSpec(StrictSchema):
    wind_zone: Literal["I", "II", "III", "IV", "V", "VI"]
    basic_wind_speed_ms: float
    terrain_category: int = Field(2, ge=1, le=4)
    risk_coefficient_k1: float = 1.0
    design_wind_pressure_kpa: float
    mast_height_m: float


class SoilFoundationSpec(StrictSchema):
    fao_soil_class: str
    soil_risk_multiplier: float
    estimated_bearing_capacity_kpa: float
    recommended_foundation_type: str
    foundation_depth_m: float


class TypologyStructuralData(StrictSchema):
    asset_typology: AssetTypology
    rationale: str
    design_load_kw: float = Field(..., gt=0.0)
    tenancy_capacity_max: int = Field(..., ge=1)
    wind_zone_spec: WindZoneSpec
    soil_foundation: SoilFoundationSpec


TypologyStructuralResult = EngineResult[TypologyStructuralData]


# ---------------------------------------------------------------------------
# Engine 5: Zero-Diesel Solar/LiFePO4 & Carbon/ESG Abatement Ledger
# ---------------------------------------------------------------------------
class EnergyEsgData(StrictSchema):
    solar_pvout_kwh_kwp_day: float
    site_load_kw: float
    recommended_solar_pv_kwp: float
    lifepo4_battery_autonomy_hours: int
    lifepo4_battery_kwh: float
    dg_baseline_share: float = Field(..., ge=0.0, le=1.0)
    annual_diesel_saved_liters: int = Field(..., ge=0)
    co2_offset_tons_per_year: float = Field(..., ge=0.0)
    ten_year_co2_offset_tons: float = Field(..., ge=0.0)
    annual_diesel_cost_saved_inr: int = Field(..., ge=0)
    zero_diesel_ready: bool = True


EnergyEsgResult = EngineResult[EnergyEsgData]


# ---------------------------------------------------------------------------
# Engine 6: Statutory & Right-of-Way (RoW) Gate
# ---------------------------------------------------------------------------
class StatutoryGateItem(StrictSchema):
    category: str
    status: Literal["CLEAR", "WARNING", "HOLD"]
    nearest_m: Optional[float] = None
    feature_name: Optional[str] = None
    hold_threshold_m: float
    warn_threshold_m: float
    authority: str


class StatutoryRowData(StrictSchema):
    overall_gate_status: Literal["CLEAR", "CONDITIONAL_ROW", "HOLD_CLEARANCE_REQUIRED"]
    gates: dict[str, StatutoryGateItem]
    blocking_categories: list[str] = Field(default_factory=list)
    warning_categories: list[str] = Field(default_factory=list)
    gatishakti_row_eligible: bool


StatutoryRowResult = EngineResult[StatutoryRowData]


# ---------------------------------------------------------------------------
# Engine 7: Beyond-Tenancy (Micro-Edge Compute & EV Charging)
# ---------------------------------------------------------------------------
class BeyondTenancySubScore(StrictSchema):
    score: int = Field(..., ge=0, le=100)
    tier: Literal["HIGH", "MODERATE", "LOW"]
    suitable: bool
    drivers: list[str] = Field(default_factory=list)
    estimated_monthly_revenue_inr: int = 0


class BeyondTenancyData(StrictSchema):
    micro_edge_compute: BeyondTenancySubScore
    ev_charging_colocation: BeyondTenancySubScore
    composite_beyond_tenancy_score: int = Field(..., ge=0, le=100)
    recommended_ancillary_streams: list[str] = Field(default_factory=list)
    estimated_total_ancillary_monthly_inr: int = Field(..., ge=0)


BeyondTenancyResult = EngineResult[BeyondTenancyData]


# ---------------------------------------------------------------------------
# Engine 8: CFO Capex/Opex BOQ & Multi-Tenant (+ Edge) IRR Engine
# ---------------------------------------------------------------------------
class CapexBoqLakh(StrictSchema):
    civil_foundation_lakh: float
    tower_structure_lakh: float
    solar_lifepo4_power_lakh: float
    backhaul_lakh: float
    edge_ev_addon_capex_lakh: float = 0.0
    total_capex_lakh: float


class OpexMonthlyInr(StrictSchema):
    site_lease_monthly_inr: int
    om_security_monthly_inr: int
    grid_solar_opex_monthly_inr: int
    backhaul_opex_monthly_inr: int
    total_monthly_opex_inr: int
    annual_opex_lakh: float


class IrrScenario(StrictSchema):
    scenario_label: str
    tenancy_ratio: float
    includes_edge_ev: bool = False
    monthly_revenue_inr: int
    monthly_ebitda_inr: int
    annual_ebitda_lakh: float
    irr_10yr_pct: Optional[float] = None
    npv_lakh: Optional[float] = None
    payback_years: Optional[float] = None


class FinancialBoqData(StrictSchema):
    capex_boq_lakh: CapexBoqLakh
    opex_monthly_inr: OpexMonthlyInr
    ancillary_beyond_tenancy_monthly_inr: int = Field(..., ge=0)
    irr_sensitivity: dict[str, IrrScenario]


FinancialBoqResult = EngineResult[FinancialBoqData]


# ---------------------------------------------------------------------------
# Engine 9: Surgical Field Verification Checklist (SOA)
# ---------------------------------------------------------------------------
class SoaActionItem(StrictSchema):
    item_id: str
    priority: Literal["CRITICAL", "HIGH", "MEDIUM"]
    category: Literal[
        "GEO_CIVIL",
        "BACKHAUL_LOS",
        "STATUTORY_ROW",
        "POWER_ESG",
        "TENANCY_EDGE",
    ]
    action_title: str
    verification_protocol: str
    trigger_evidence: str
    owner_role: str


class SoaChecklistData(StrictSchema):
    items: list[SoaActionItem] = Field(..., min_length=3, max_length=5)
    critical_count: int = Field(..., ge=0)
    estimated_field_hours: float = Field(..., gt=0.0)
    sla_target_hours: float = 48.0


SoaChecklistResult = EngineResult[SoaChecklistData]


# ---------------------------------------------------------------------------
# Master Report & Batch Triage Schemas
# ---------------------------------------------------------------------------
class MasterEnginesV2(StrictSchema):
    spatial_base: SpatialBaseResult
    ring_optimizer: RingOptimizerResult
    backhaul_los: BackhaulLosResult
    typology_and_structural: TypologyStructuralResult
    energy_esg: EnergyEsgResult
    statutory_row: StatutoryRowResult
    beyond_tenancy: BeyondTenancyResult
    financial_boq: FinancialBoqResult
    soa_checklist: SoaChecklistResult


class MasterReportV2(StrictSchema):
    schema_version: Literal["2.0"] = "2.0"
    site_id: str = Field(..., min_length=1)
    input_lat: float = Field(..., ge=-90.0, le=90.0)
    input_lon: float = Field(..., ge=-180.0, le=180.0)
    search_radius_m: float = Field(..., gt=0.0, le=5000.0)
    generated_at: str = Field(..., min_length=1)
    overall_score: float = Field(..., ge=0.0, le=100.0)
    verdict: OverallVerdict
    hero_cards: list[HeroCard] = Field(..., min_length=3, max_length=3)
    engines: MasterEnginesV2
    peak_rss_mb: float = Field(..., ge=0.0)

    @model_validator(mode="after")
    def _validate_hero_card_order(self) -> "MasterReportV2":
        actual = tuple(card.card_id for card in self.hero_cards)
        if actual != HERO_CARD_ORDER:
            raise ValueError(
                f"hero_cards must match exact order {list(HERO_CARD_ORDER)}, got {list(actual)}"
            )
        return self


class BatchColumnMappingSummary(StrictSchema):
    detected_mapping: dict[str, Optional[str]] = Field(default_factory=dict)
    unmapped_headers: list[str] = Field(default_factory=list)
    preserved_headers: list[str] = Field(default_factory=list)


class BatchTriageSummaryV2(StrictSchema):
    total_rings: int = Field(..., ge=0)
    green_count: int = Field(..., ge=0)
    amber_count: int = Field(..., ge=0)
    red_count: int = Field(..., ge=0)
    total_capex_crore: float
    avg_irr_1_5x_pct: float
    avg_irr_2_0x_edge_pct: Optional[float] = None
    total_co2_offset_tpy: float
    peak_rss_mb: float = Field(..., ge=0.0)
    memory_ceiling_mb: float = 1536.0


class BatchTriageSiteV2(StrictSchema):
    site_id: str
    status: Literal["Green", "Amber", "Red"]
    rank_score: int = Field(..., ge=0, le=100)
    overall_score: float = Field(..., ge=0.0, le=100.0)
    verdict: OverallVerdict
    input_lat: float
    input_lon: float
    opt_lat: float
    opt_lon: float
    meters_shifted: float
    elevation_m: Optional[float] = None
    capex_saved_lakh: float
    state: str
    typology: str
    statutory_gate: str
    bharatnet_ofc_m: Optional[float] = None
    nearest_power_m: Optional[float] = None
    solar_pv_kwp: float
    co2_offset_tpy: float
    beyond_tenancy_score: int
    total_capex_lakh: float
    monthly_opex_inr: int
    irr_1_0x_pct: Optional[float] = None
    irr_1_5x_pct: Optional[float] = None
    irr_2_0x_pct: Optional[float] = None
    irr_2_0x_edge_pct: Optional[float] = None
    data_sources: list[str] = Field(default_factory=list)
    original_row: dict[str, Any] = Field(default_factory=dict)


class BatchTriageResponseV2(StrictSchema):
    schema_version: Literal["2.0"] = "2.0"
    column_mapping: BatchColumnMappingSummary
    summary: BatchTriageSummaryV2
    sites: list[BatchTriageSiteV2]


# ---------------------------------------------------------------------------
# API v2 Request / Response, GIS, Regulatory Pre-Fill & Webhook Schemas
# ---------------------------------------------------------------------------
class HealthV2Response(StrictSchema):
    status: Literal["ok", "degraded"]
    version: str = "2.0.0"
    sqlite_ready: bool
    schema_verified: bool
    tables_found: list[str] = Field(default_factory=list)
    raster_deps_available: bool
    peak_rss_mb: float = Field(..., ge=0.0)
    memory_ceiling_mb: float = 1536.0


class FeasibilityV2Request(StrictSchema):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    radius_km: float = Field(5.0, ge=0.2, le=25.0)
    search_radius_m: float = Field(
        500.0,
        ge=100.0,
        le=2000.0,
        validation_alias=AliasChoices("search_radius_m", "ring_radius_m"),
    )
    grid_points: int = Field(25, ge=9, le=25)
    site_id: str = Field("SITE-001", min_length=1)
    ant_height_m: float = Field(35.0, ge=10.0, le=100.0)


class RingOptimizeV2Request(StrictSchema):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    radius_m: float = Field(
        500.0,
        ge=100.0,
        le=2000.0,
        validation_alias=AliasChoices("radius_m", "search_radius_m", "ring_radius_m"),
    )
    grid_points: int = Field(25, ge=9, le=25)
    fao_soil: Optional[str] = None


class BackhaulLosV2Request(StrictSchema):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    radius_km: float = Field(15.0, ge=1.0, le=30.0)
    ant_height_m: float = Field(35.0, ge=10.0, le=100.0)
    num_profile_points: int = Field(30, ge=10, le=60)


class FinancialRecalcV2Request(StrictSchema):
    typology: str = "Macro GBT"
    slope_pct: float = Field(2.0, ge=0.0)
    soil_risk_multiplier: float = Field(1.0, gt=0.0)
    solar_pv_kwp: float = Field(24.0, ge=0.0)
    battery_kwh: float = Field(80.0, ge=0.0)
    fttt_capex_lakh: Optional[float] = 5.0
    backhaul_mode: str = "FIBER_FTTT_PRIMARY"
    beyond_tenancy_score: int = Field(50, ge=0, le=100)
    edge_ev_capex_lakh: float = Field(0.0, ge=0.0)
    overrides: dict[str, Any] = Field(default_factory=dict)


class GisExportRequest(StrictSchema):
    report: Optional[MasterReportV2] = None
    site_id: str = "SITE-001"
    lat: Optional[float] = Field(None, ge=-90.0, le=90.0)
    lon: Optional[float] = Field(None, ge=-180.0, le=180.0)
    search_radius_m: float = Field(
        500.0,
        ge=100.0,
        le=2000.0,
        validation_alias=AliasChoices("search_radius_m", "ring_radius_m"),
    )
    include_los_lines: bool = True
    include_search_ring: bool = True

    @model_validator(mode="after")
    def _require_report_or_coords(self) -> "GisExportRequest":
        if self.report is None and (self.lat is None or self.lon is None):
            raise ValueError("Either 'report' or both 'lat' and 'lon' must be provided.")
        return self


class GeoJsonGeometry(StrictSchema):
    type: Literal["Point", "LineString", "Polygon"]
    coordinates: Any


class GeoJsonFeature(StrictSchema):
    type: Literal["Feature"] = "Feature"
    id: Optional[str] = None
    geometry: GeoJsonGeometry
    properties: dict[str, Any] = Field(default_factory=dict)


class GeoJsonFeatureCollection(StrictSchema):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[GeoJsonFeature]
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegulatoryPrefillRequest(StrictSchema):
    report: Optional[MasterReportV2] = None
    site_id: str = "SITE-001"
    lat: Optional[float] = Field(None, ge=-90.0, le=90.0)
    lon: Optional[float] = Field(None, ge=-180.0, le=180.0)
    search_radius_m: float = Field(
        500.0,
        ge=100.0,
        le=2000.0,
        validation_alias=AliasChoices("search_radius_m", "ring_radius_m"),
    )
    applicant_ip1_name: str = "IP-1 Neutral Host Infra Pvt. Ltd."
    proposed_mast_height_m: Optional[float] = Field(None, gt=0.0, le=120.0)

    @model_validator(mode="after")
    def _require_report_or_coords(self) -> "RegulatoryPrefillRequest":
        if self.report is None and (self.lat is None or self.lon is None):
            raise ValueError("Either 'report' or both 'lat' and 'lon' must be provided.")
        return self


class GatiShaktiRowPrefill(StrictSchema):
    portal: Literal["PM_GATISHAKTI_ROW"] = "PM_GATISHAKTI_ROW"
    site_id: str
    applicant_ip1_name: str
    state: str
    latitude: float
    longitude: float
    asset_typology: str
    trench_length_m: Optional[float] = None
    bharatnet_tie_in_distance_m: Optional[float] = None
    overall_gate_status: str
    required_noc_authorities: list[str] = Field(default_factory=list)
    proximity_clearances: dict[str, Any] = Field(default_factory=dict)
    submission_ready: bool


class SacfaWpcPrefill(StrictSchema):
    portal: Literal["WPC_SACFA"] = "WPC_SACFA"
    site_id: str
    applicant_ip1_name: str
    latitude: float
    longitude: float
    ground_elevation_amsl_m: Optional[float] = None
    proposed_structure_height_agl_m: float
    total_height_amsl_m: Optional[float] = None
    nearest_aviation_feature: Optional[str] = None
    nearest_aviation_distance_m: Optional[float] = None
    aai_noc_required: bool
    backhaul_bands_requested_ghz: list[float] = Field(default_factory=list)
    wind_zone: str
    design_wind_speed_ms: float


class RegulatoryPrefillResponse(StrictSchema):
    schema_version: Literal["2.0"] = "2.0"
    site_id: str
    generated_at: str
    gatishakti_row: GatiShaktiRowPrefill
    sacfa_wpc: SacfaWpcPrefill
    evidence_refs: list[str] = Field(default_factory=list)


class WebhookDispatchRequest(StrictSchema):
    target_system: WebhookTargetSystem
    webhook_url: str = Field(..., min_length=1)
    site_id: str = "SITE-001"
    report: Optional[MasterReportV2] = None
    lat: Optional[float] = Field(None, ge=-90.0, le=90.0)
    lon: Optional[float] = Field(None, ge=-180.0, le=180.0)
    search_radius_m: float = Field(
        500.0,
        ge=100.0,
        le=2000.0,
        validation_alias=AliasChoices("search_radius_m", "ring_radius_m"),
    )
    callback_url: Optional[str] = None
    auth_token: Optional[str] = None
    hmac_secret: Optional[str] = None
    custom_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_report_or_coords(self) -> "WebhookDispatchRequest":
        if self.report is None and (self.lat is None or self.lon is None):
            raise ValueError("Either 'report' or both 'lat' and 'lon' must be provided.")
        return self


class WebhookDispatchResponse(StrictSchema):
    delivery_id: str
    target_system: WebhookTargetSystem
    webhook_url: str
    delivered: bool
    http_status: Optional[int] = None
    signature_header: Optional[str] = None
    payload_sent: dict[str, Any]
    response_body: Optional[str] = None
    error: Optional[str] = None
    dispatched_at: str


class WebhookCallbackPayload(StrictSchema):
    delivery_id: str = Field(..., min_length=1)
    target_system: WebhookTargetSystem
    site_id: str
    external_record_id: Optional[str] = None
    status: Literal["ACKNOWLEDGED", "IN_PROGRESS", "APPROVED", "REJECTED", "FAILED"]
    notes: Optional[str] = None
    received_at: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WebhookCallbackResponse(StrictSchema):
    accepted: bool
    delivery_id: str
    verified_signature: bool
    recorded_status: str
    processed_at: str
