"""Runtime configuration. Secrets come from env vars populated by Secret Manager on Cloud Run."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Gemini on Vertex AI (uses the Cloud Run service account; no API key)
    gcp_project: str = ""
    gcp_location: str = "global"
    gemini_model: str = "gemini-3-flash-preview"
    # Shopify Agentic Storefront (UCP MCP endpoint, public)
    shop_domain: str = "team-fourcast.myshopify.com"
    ucp_agent_profile: str = "https://shopify.dev/ucp/agent-profiles/examples/2026-08-25/valid-with-capabilities.json"
    # Bloomreach Engagement
    bloomreach_base_url: str = "https://api-engagement.bloomreach.com"
    bloomreach_project_token: str = ""
    bloomreach_api_key_id: str = ""
    bloomreach_api_secret: str = ""
    recommendation_id: str = "6ab18fe82b2f5cbab434716a"   # "T6 Personalized Picks"
    # State + features
    state_backend: str = "firestore"          # firestore | memory | databricks
    state_collection: str = "journeys"
    features_backend: str = "bloomreach"      # bloomreach | databricks | none
    databricks_host: str = ""
    databricks_token: str = ""
    databricks_warehouse_id: str = ""
    databricks_features_table: str = "`databricks-hackathon`.fourcast.customer_profile"
    # Decision policy
    retention_discount_code: str = ""          # e.g. STAYCOZY10, must exist in Shopify Discounts
    churn_risk_threshold: float = 0.6
    max_tool_rounds: int = 8
    # Return the customer profile summary (LTV, churn risk, offer) to the chat UI's side panel.
    # Handy for the hackathon demo; set EXPOSE_PROFILE=false before real shoppers use it.
    expose_profile: bool = True


def load_settings() -> Settings:
    e = os.environ.get
    return Settings(
        gcp_project=e("GOOGLE_CLOUD_PROJECT", ""),
        gcp_location=e("GOOGLE_CLOUD_LOCATION", "global"),
        gemini_model=e("GEMINI_MODEL", "gemini-3-flash-preview"),
        shop_domain=e("SHOP_DOMAIN", "team-fourcast.myshopify.com"),
        ucp_agent_profile=e("UCP_AGENT_PROFILE", Settings.ucp_agent_profile),
        bloomreach_base_url=e("BLOOMREACH_BASE_URL", "https://api-engagement.bloomreach.com"),
        bloomreach_project_token=e("BLOOMREACH_PROJECT_TOKEN", ""),
        bloomreach_api_key_id=e("BLOOMREACH_API_KEY_ID", ""),
        bloomreach_api_secret=e("BLOOMREACH_API_SECRET", ""),
        recommendation_id=e("BLOOMREACH_RECOMMENDATION_ID", Settings.recommendation_id),
        state_backend=e("STATE_BACKEND", "firestore"),
        state_collection=e("STATE_COLLECTION", "journeys"),
        features_backend=e("FEATURES_BACKEND", "bloomreach"),
        databricks_host=e("DATABRICKS_HOST", ""),
        databricks_token=e("DATABRICKS_TOKEN", ""),
        databricks_warehouse_id=e("DATABRICKS_WAREHOUSE_ID", ""),
        databricks_features_table=e("DATABRICKS_FEATURES_TABLE", Settings.databricks_features_table),
        retention_discount_code=e("RETENTION_DISCOUNT_CODE", ""),
        churn_risk_threshold=float(e("CHURN_RISK_THRESHOLD", "0.6")),
        expose_profile=e("EXPOSE_PROFILE", "true").lower() not in ("0", "false", "no"),
    )
