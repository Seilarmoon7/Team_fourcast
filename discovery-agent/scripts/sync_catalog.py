#!/usr/bin/env python3
"""Rebuild app/catalog.json from the live Shopify store after importing the 5,000-item CSV.

The agent searches catalog.json (room / style / color / material / type) and then asks Shopify for
live price + stock by variant id. New products only get variant ids once Shopify has created them,
so run this after each import (Cloud Shell):

    cd ~/discovery-agent && python3 scripts/sync_catalog.py      # asks for the storefront password
    gcloud run deploy t6-discovery-agent --source . --region us-central1
"""
import getpass, json, re, sys, time
from pathlib import Path
import httpx

SHOP = "team-fourcast.myshopify.com"
HERE = Path(__file__).resolve().parent
ATTRS = {i["sku"]: i for i in json.loads((HERE / "nordhem_catalog_5000.json").read_text())}
OUT = HERE.parent / "app" / "catalog.json"


def main() -> None:
    old = json.loads(OUT.read_text()) if OUT.exists() else []
    c = httpx.Client(base_url=f"https://{SHOP}", follow_redirects=True, timeout=30,
                     headers={"User-Agent": "t6-catalog-sync"})
    r = c.get("/products.json?limit=1")
    if "/password" in str(r.url) or r.headers.get("content-type", "").startswith("text/html"):
        pw = getpass.getpass("Storefront password (Online Store > Preferences): ")
        c.post("/password", data={"form_type": "storefront_password", "utf8": "✓", "password": pw})
    by_sku, page = {}, 1
    while True:
        r = c.get(f"/products.json?limit=250&page={page}")
        if r.status_code == 429:
            time.sleep(2); continue
        r.raise_for_status()
        prods = r.json().get("products", [])
        if not prods:
            break
        for p in prods:
            for v in p.get("variants", []):
                if v.get("sku"):
                    by_sku[v["sku"]] = (p, v)
        print(f"page {page}: {len(prods)} products", file=sys.stderr)
        page += 1
    new, missing = [], 0
    for sku, a in ATTRS.items():
        if sku not in by_sku:
            missing += 1
            continue
        p, v = by_sku[sku]
        plain = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", a["description"])).strip()
        new.append({"item_id": sku, "title": a["title"], "price": float(v.get("price") or a["price"]),
                    "category_level_1": a["category_level_1"], "category_level_2": a["type"],
                    "room": a["room"], "style": a["style"], "color": a["color"], "material": a["material"],
                    "price_tier": a["price_tier"],
                    "tags": [a["type"].lower(), a["style"], a["color"], a["material"], a["room"], a["price_tier"]],
                    "description": plain, "image": (p.get("images") or [{}])[0].get("src"),
                    "variant_id": str(v["id"])})
    keep = [o for o in old if o["item_id"] not in ATTRS]      # the original 116 curated items
    OUT.write_text(json.dumps(keep + new, indent=0))
    print(f"catalog.json: {len(keep)} existing + {len(new)} new = {len(keep) + len(new)} items"
          + (f"  ({missing} not found in Shopify yet: import still running?)" if missing else ""))


if __name__ == "__main__":
    main()
