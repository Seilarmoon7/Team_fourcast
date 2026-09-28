"""Customer intelligence that grounds the agent's decisions.

* BloomreachFeatures (today): profile properties the agent/webhook maintain in Bloomreach
  (name, city, saved style/budget) + the personalized recommendation list.
* DatabricksFeatures (when the workspace is provisioned): reads the Lakehouse feature table
  (style affinity, avg order value, predicted LTV, churn risk) through the SQL Statement API,
  then merges in Bloomreach's recommendations.
"""
from __future__ import annotations

import concurrent.futures
import logging

import httpx

from .bloomreach import Bloomreach

log = logging.getLogger("discovery-agent")

PROFILE_PROPS = ["first_name", "city", "style_preference", "budget_max", "room_focus"]


def _empty(email: str | None) -> dict:
    return {"email": email, "known_customer": False, "first_name": None, "city": None,
            "style_preference": None, "budget_max": None, "avg_order_value": None,
            "predicted_ltv": None, "churn_risk": None, "orders_count": None,
            "recommended_skus": [], "source": []}


class BloomreachFeatures:
    def __init__(self, br: Bloomreach) -> None:
        self.br = br

    def get(self, email: str | None) -> dict:
        f = _empty(email)
        if not email:
            return f
        try:
            a = self.br.customer_attributes(email, PROFILE_PROPS)
        except Exception as e:  # never let a read failure kill the conversation
            log.warning("bloomreach_features_failed %s", e)
            return f
        p = a["properties"]
        f.update({k: p.get(k) for k in ("first_name", "city", "style_preference", "budget_max")})
        f["known_customer"] = any(v for v in p.values())
        f["recommended_skus"] = a["recommendations"]
        f["source"].append("bloomreach")
        return f


# Columns of `databricks-hackathon`.fourcast.customer_profile (view over 00data.customer_360)
DBX_COLUMNS = ["customer_id", "first_name", "predicted_ltv", "avg_order_value", "churn_risk",
               "purchase_propensity_score", "loyalty_tier", "segment", "marketing_opt_in",
               "purchases_90d", "days_since_last_purchase", "value_tier", "offer_eligible"]


def map_customer_profile(row: dict) -> dict:
    """Databricks row (all values arrive as strings) -> the agent's feature names."""
    num = lambda v: None if v in (None, "") else float(v)
    boolean = lambda v: None if v in (None, "") else str(v).lower() == "true"
    out = {
        "customer_id": row.get("customer_id"),
        "first_name": row.get("first_name"),
        "predicted_ltv": num(row.get("predicted_ltv")),
        "avg_order_value": num(row.get("avg_order_value")),
        "churn_risk": num(row.get("churn_risk")),
        "propensity": num(row.get("purchase_propensity_score")),
        "loyalty_tier": row.get("loyalty_tier"),
        "segment": row.get("segment"),
        "marketing_opt_in": boolean(row.get("marketing_opt_in")),
        "orders_count": int(row["purchases_90d"]) if row.get("purchases_90d") not in (None, "") else None,
        "days_since_last_purchase": int(row["days_since_last_purchase"])
        if row.get("days_since_last_purchase") not in (None, "") else None,
        "value_tier": row.get("value_tier"),
        "offer_eligible": boolean(row.get("offer_eligible")),
    }
    return {k: v for k, v in out.items() if v is not None}


class DatabricksFeatures:
    """SELECT one row from the Lakehouse feature table via the Databricks SQL Statement API."""

    # wait_timeout bounds the SQL warehouse round trip; the http timeout is kept a couple
    # seconds above it so the server's own CANCEL response wins over a client-side timeout.
    WAIT_TIMEOUT_S = "4s"

    def __init__(self, host: str, token: str, warehouse_id: str, table: str, fallback: BloomreachFeatures,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._url = f"https://{host.removeprefix('https://').rstrip('/')}/api/2.0/sql/statements"
        self._http = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=6.0, transport=transport)
        self._wh, self._table, self._fallback = warehouse_id, table, fallback

    def get(self, email: str | None) -> dict:
        if not email:
            return self._fallback.get(email)
        # Bloomreach and Databricks don't depend on each other - fetch them at the same time
        # instead of paying both round trips back to back.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            fallback_fut = ex.submit(self._fallback.get, email)
            dbx_row = None
            try:
                r = self._http.post(self._url, json={
                    "warehouse_id": self._wh, "wait_timeout": self.WAIT_TIMEOUT_S, "on_wait_timeout": "CANCEL",
                    "statement": f"SELECT {', '.join(DBX_COLUMNS)} FROM {self._table} WHERE email = :email LIMIT 1",
                    "parameters": [{"name": "email", "value": email.lower()}]})
                body = r.json()
                state = (body.get("status") or {}).get("state")
                rows = (body.get("result") or {}).get("data_array") or []
                if r.status_code != 200 or state not in (None, "SUCCEEDED"):
                    log.warning("databricks_features_failed status=%s state=%s err=%s", r.status_code, state,
                                (body.get("status") or {}).get("error") or body.get("message"))
                elif rows:
                    cols = [c["name"] for c in body["manifest"]["schema"]["columns"]]
                    dbx_row = dict(zip(cols, rows[0]))
            except Exception as e:
                log.warning("databricks_features_failed %s", e)
            f = fallback_fut.result()
        if dbx_row is not None:
            f.update(map_customer_profile(dbx_row))
            f["known_customer"] = True
            f["source"].append("databricks")
        return f
