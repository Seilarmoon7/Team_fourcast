"""Customer intelligence that grounds the agent's decisions.

* BloomreachFeatures (today): profile properties the agent/webhook maintain in Bloomreach
  (name, city, saved style/budget) + the personalized recommendation list.
* DatabricksFeatures (when the workspace is provisioned): reads the Lakehouse feature table
  (style affinity, avg order value, predicted LTV, churn risk) through the SQL Statement API,
  then merges in Bloomreach's recommendations.
"""
from __future__ import annotations

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


class DatabricksFeatures:
    """SELECT one row from the Lakehouse feature table via the Databricks SQL Statement API."""

    def __init__(self, host: str, token: str, warehouse_id: str, table: str, fallback: BloomreachFeatures,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._url = f"https://{host.removeprefix('https://').rstrip('/')}/api/2.0/sql/statements"
        self._http = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=10.0, transport=transport)
        self._wh, self._table, self._fallback = warehouse_id, table, fallback

    def get(self, email: str | None) -> dict:
        f = self._fallback.get(email)
        if not email:
            return f
        try:
            r = self._http.post(self._url, json={
                "warehouse_id": self._wh, "wait_timeout": "10s", "on_wait_timeout": "CANCEL",
                "statement": f"SELECT style_preference, budget_max, avg_order_value, predicted_ltv, churn_risk, "
                             f"orders_count FROM {self._table} WHERE email = :email LIMIT 1",
                "parameters": [{"name": "email", "value": email.lower()}]})
            body = r.json()
            rows = (body.get("result") or {}).get("data_array") or []
            if r.status_code == 200 and rows:
                cols = [c["name"] for c in body["manifest"]["schema"]["columns"]]
                row = dict(zip(cols, rows[0]))
                for k in ("avg_order_value", "predicted_ltv", "churn_risk", "budget_max"):
                    if row.get(k) is not None:
                        row[k] = float(row[k])
                if row.get("orders_count") is not None:
                    row["orders_count"] = int(row["orders_count"])
                f.update({k: v for k, v in row.items() if v is not None})
                f["known_customer"] = True
                f["source"].append("databricks")
        except Exception as e:
            log.warning("databricks_features_failed %s", e)
        return f
