import json

import httpx
import pytest
import respx
from google.genai import types

from app.agent import BY_SKU, DiscoveryAgent, Toolbox
from app.bloomreach import Bloomreach
from app.config import Settings
from app.features import BloomreachFeatures
from app.shopify import ShopifyStorefront
from app.state import Journey, MemoryJourneyStore

SHOP = "team-fourcast.myshopify.com"
UCP = f"https://{SHOP}/api/ucp/mcp"
BR = "https://api-engagement.bloomreach.com"


def ucp_response(request):
    body = json.loads(request.content)
    ids = body["params"]["arguments"]["catalog"].get("ids", [])
    products = []
    for gid in ids:
        vid = gid.rsplit("/", 1)[-1]
        sku = next(c["item_id"] for c in BY_SKU.values() if c["variant_id"] == vid)
        c = BY_SKU[sku]
        products.append({"title": c["title"], "variants": [{
            "id": gid, "sku": sku, "price": {"amount": int(round(c["price"] * 100)), "currency": "USD"},
            "availability": {"available": sku != "lr-002"}, "checkout_url": f"https://{SHOP}/cart/{vid}:1"}]})
    text = json.dumps({"ucp": {}, "products": products})
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": text}]}})


def br_attrs(churn=None):
    def handler(request):
        attrs = json.loads(request.content)["attributes"]
        res = []
        for a in attrs:
            if a["type"] == "recommendation":
                res.append({"success": True, "value": [{"item_id": "lr-008"}, {"item_id": "lr-029"}]})
            else:
                res.append({"success": True, "value": {"first_name": "Aisuluu", "style_preference": "boho"}.get(a["property"])})
        return httpx.Response(200, json={"success": True, "results": res})
    return handler


class FakeModels:
    """Replays a script of model turns; each item is a list of Parts."""
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def generate_content(self, model, contents, config):
        self.calls.append(contents)
        parts = self.script.pop(0)
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=parts))])


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


def fc(name, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def make(script, settings=None):
    s = settings or Settings()
    shop = ShopifyStorefront(SHOP, s.ucp_agent_profile)
    br = Bloomreach(BR, "tok", "kid", "sec", s.recommendation_id)
    return DiscoveryAgent(FakeClient(script), s, shop, br, BloomreachFeatures(br))


@pytest.fixture
def mocks():
    with respx.mock(assert_all_called=False) as m:
        m.post(UCP).mock(side_effect=ucp_response)
        attrs = m.post(f"{BR}/data/v2/projects/tok/customers/attributes").mock(side_effect=br_attrs())
        track = m.post(f"{BR}/track/v2/projects/tok/batch").mock(
            side_effect=lambda r: httpx.Response(200, json={"results": [{"success": True}] * len(json.loads(r.content)["commands"])}))
        yield {"attrs": attrs, "track": track, "router": m}


def tracked_types(track):
    out = []
    for call in track.calls:
        for c in json.loads(call.request.content)["commands"]:
            out.append(c["data"].get("event_type", "profile"))
    return out


def test_full_journey_discovery_to_checkout(mocks):
    agent = make([
        [fc("get_customer_profile"), fc("save_preferences", room="living room", style="scandinavian", budget_max=1500)],
        [fc("find_products", keywords="sofa rug", max_price=1000, limit=3)],
        [types.Part(text="Here are three Scandinavian picks within budget.")],
        [fc("create_checkout", items=[{"sku": "lr-001", "quantity": 1}])],
        [types.Part(text="Your checkout is ready.")],
    ])
    store = MemoryJourneyStore()
    j = Journey(email="shopper@example.com")
    out1 = agent.turn(j, "I need to redecorate my living room, scandi, $1500")
    store.save(j)
    assert out1["reply"].startswith("Here are three")
    assert 0 < len(out1["products"]) <= 3
    assert all(p["price"] <= 1000 for p in out1["products"])
    assert "lr-002" not in [p["sku"] for p in out1["products"]]           # sold out on Shopify -> hidden
    assert j.prefs["style"] == "scandinavian" and j.stage == "recommending"

    j2 = store.load(j.session_id)                                           # new request, state reloaded
    out2 = agent.turn(j2, "I'll take the sofa")
    from urllib.parse import parse_qs, unquote, urlparse
    u = urlparse(out2["checkout_url"])
    assert u.path == "/cart/clear"                                   # empties any stale cart first
    cart = unquote(parse_qs(u.query)["return_to"][0])
    assert cart.startswith("/cart/51799216422948:1?")
    assert "checkout[email]=" not in cart      # not valid on a /cart/... permalink, breaks the discount too
    assert "attributes[t6_session]=" + j.session_id in cart
    assert "discount=" not in cart                          # no churn signal -> no discount
    types_ = tracked_types(mocks["track"])
    assert "view_item" in types_ and "search" in types_ and "cart_update" in types_ and "checkout" in types_
    # history persisted with function calls + responses and replayed on the 2nd turn
    assert any(p.function_response for c in agent.client.models.calls[-1] for p in (c.parts or []))


def test_checkout_requires_email(mocks):
    agent = make([[fc("create_checkout", items=[{"sku": "lr-001"}])], [types.Part(text="What's your email?")]])
    j = Journey()
    out = agent.turn(j, "buy the sofa")
    assert out["checkout_url"] is None
    last_fn = [p for c in agent.client.models.calls[-1] for p in (c.parts or []) if p.function_response][-1]
    assert last_fn.function_response.response["result"]["error"] == "need_email"


def test_discount_only_for_high_churn_risk(mocks):
    s = Settings(retention_discount_code="STAYCOZY10", churn_risk_threshold=0.6)
    agent = make([], s)
    class F:  # feature provider stub standing in for Databricks
        def __init__(self, risk): self.risk = risk
        def get(self, email): return {"churn_risk": self.risk, "recommended_skus": []}
    hi = Toolbox(Journey(email="a@b.co"), agent.shop, agent.br, F(0.8), s).create_checkout([{"sku": "lr-001"}])
    lo = Toolbox(Journey(email="a@b.co"), agent.shop, agent.br, F(0.2), s).create_checkout([{"sku": "lr-001"}])
    assert hi["discount_code"] == "STAYCOZY10" and "discount%3DSTAYCOZY10" in hi["checkout_url"]
    assert lo["discount_code"] is None


def test_personal_picks_come_from_bloomreach(mocks):
    agent = make([])
    j = Journey(email="a@b.co")
    out = Toolbox(j, agent.shop, agent.br, agent.features, agent.s).get_personal_picks(limit=2)
    assert [p["sku"] for p in out["products"]] == ["lr-008", "lr-029"]


def test_unknown_tool_and_bad_args_are_contained(mocks):
    agent = make([])
    t = Toolbox(Journey(), agent.shop, agent.br, agent.features, agent.s)
    assert "error" in t.run("drop_tables", {})
    assert "error" in t.run("find_products", {"nope": 1})


def test_shopify_down_returns_no_products_not_crash(mocks):
    mocks["router"].post(UCP).mock(return_value=httpx.Response(503))
    agent = make([])
    out = Toolbox(Journey(), agent.shop, agent.br, agent.features, agent.s).find_products("sofa")
    assert out["products"] == []


def test_find_products_batches_shopify_lookups_under_10_ids(mocks):
    """Shopify's real lookup_catalog rejects >10 ids/call; find_products must never send that many."""
    calls = []

    def capped(request):
        body = json.loads(request.content)
        ids = body["params"]["arguments"]["catalog"].get("ids", [])
        calls.append(len(ids))
        if len(ids) > 10:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "isError": True, "content": [{"type": "text",
                "text": "Invalid arguments: array size at `/catalog/ids` is greater than: 10"}]}})
        return ucp_response(request)

    mocks["router"].post(UCP).mock(side_effect=capped)
    agent = make([])
    t = Toolbox(Journey(email="a@b.co"), agent.shop, agent.br, agent.features, agent.s)
    out = t.find_products(room="living room", limit=6)   # limit*3=18 candidates -> must batch into >=2 calls
    assert not out.get("error")
    assert len(out["products"]) == 6
    assert all(n <= 10 for n in calls)
    assert len(calls) >= 2


def test_http_endpoint(monkeypatch, mocks):
    from fastapi.testclient import TestClient
    from app import main
    agent = make([[types.Part(text="Hello! Which room?")], [types.Part(text="Great, boho it is.")]])
    store = MemoryJourneyStore()
    monkeypatch.setattr(main, "get_agent", lambda: agent)
    monkeypatch.setattr(main, "get_store", lambda: store)
    c = TestClient(main.app)
    assert c.get("/health").json() == {"ok": True}
    assert "Nordhem" in c.get("/").text
    r1 = c.post("/api/chat", json={"message": "hi"}).json()
    r2 = c.post("/api/chat", json={"message": "boho", "session_id": r1["session_id"]}).json()
    assert r2["session_id"] == r1["session_id"] and r2["reply"] == "Great, boho it is."
    assert len(store.load(r1["session_id"]).history) == 4


def test_tool_budget_exhausted_still_answers(mocks):
    s = Settings(max_tool_rounds=2)
    agent = make([[fc("find_products", keywords="rug")], [fc("find_products", keywords="wool rug")],
                  [types.Part(text="Here is the best rug I found.")]], s)
    out = agent.turn(Journey(email="a@b.co"), "a rug please")
    assert out["reply"] == "Here is the best rug I found."
    assert out["products"]


def test_shopify_down_is_reported_to_model(mocks):
    mocks["router"].post(UCP).mock(return_value=httpx.Response(503))
    agent = make([])
    out = Toolbox(Journey(), agent.shop, agent.br, agent.features, agent.s).find_products("sofa")
    assert out["error"].startswith("store_unavailable")


def test_new_checkout_only_contains_latest_items(mocks):
    agent = make([
        [fc("create_checkout", items=[{"sku": "lr-012"}, {"sku": "lr-023"}])], [types.Part(text="Link ready.")],
        [fc("find_products", keywords="rug")], [types.Part(text="Here are rugs.")],
        [fc("create_checkout", items=[{"sku": "lr-044"}])], [types.Part(text="Rug link ready.")],
    ])
    j = Journey(email="a@b.co")
    first = agent.turn(j, "buy table and lamp")
    assert "51799233265700%3A1%2C51799234445348%3A1" in first["checkout_url"]
    browsing = agent.turn(j, "now show me rugs")
    assert browsing["checkout_url"] is None and j.stage != "checkout"   # no stale link resurfacing
    rug = agent.turn(j, "buy the rug")
    from urllib.parse import parse_qs, unquote, urlparse
    cart = unquote(parse_qs(urlparse(rug["checkout_url"]).query)["return_to"][0])
    assert cart.split("?")[0] == "/cart/51799269179428:1"            # only the rug, no earlier items


def _campaign_events(track):
    out = []
    for call in track.calls:
        for c in json.loads(call.request.content)["commands"]:
            if c["data"].get("event_type") == "campaign_request":
                out.append(c["data"]["properties"])
    return out


def test_agent_hands_a_campaign_goal_to_bloomreach(mocks):
    agent = make([
        [fc("find_products", keywords="sofa", limit=2)],
        [types.Part(text="Here are two sofas.")],
        [fc("request_campaign", goal="bring her back to finish the scandi living room under $1500",
            subject="Your Scandinavian living room", headline="Still thinking it over?",
            message="Here are the pieces we looked at.", skus=["lr-001"])],
        [types.Part(text="Sent to your inbox shortly.")],
    ])
    j = Journey(email="a@b.co")
    agent.turn(j, "show me sofas")
    out = agent.turn(j, "I'll think about it, email me the list")
    assert out["reply"].startswith("Sent")
    reqs = _campaign_events(mocks["track"])
    assert len(reqs) == 1
    r = reqs[0]
    assert r["channel"] == "email" and r["requested_by"] == "discovery_agent"
    assert r["goal"].startswith("bring her back")
    assert r["products"][0]["sku"] == "lr-001" and r["products"][0]["price"] == 899.0   # live Shopify price
    assert j.campaign_requested


def test_campaign_request_needs_email_and_runs_once(mocks):
    agent = make([])
    t = Toolbox(Journey(), agent.shop, agent.br, agent.features, agent.s)
    assert t.request_campaign("g", "s", "h", "m")["error"] == "need_email"
    j = Journey(email="a@b.co", shown=["lr-001"])
    t2 = Toolbox(j, agent.shop, agent.br, agent.features, agent.s)
    assert t2.request_campaign("g", "s", "h", "m", skus=["lr-001"])["handed_to"] == "bloomreach"
    assert Toolbox(j, agent.shop, agent.br, agent.features, agent.s).request_campaign(
        "g", "s", "h", "m")["error"] == "already_requested"


def test_campaign_request_strips_html_and_caps_length(mocks):
    agent = make([])
    j = Journey(email="a@b.co", shown=["lr-001"])
    Toolbox(j, agent.shop, agent.br, agent.features, agent.s).request_campaign(
        "g" * 900, "<b>Subject</b>", "h", "m")
    r = _campaign_events(mocks["track"])[-1]
    assert len(r["goal"]) == 400 and r["subject"] == "Subject"


def test_campaign_only_features_products_actually_shown(mocks):
    agent = make([])
    j = Journey(email="a@b.co", shown=["lr-044"])          # only the rug was shown
    out = Toolbox(j, agent.shop, agent.br, agent.features, agent.s).request_campaign(
        "g", "s", "h", "m", skus=["lr-044", "lr-013", "not-a-sku"])
    assert out["products"] == ["lr-044"]


def test_profile_summary_returned_for_side_panel(mocks):
    agent = make([[fc("get_customer_profile")], [types.Part(text="Welcome back!")]])
    out = agent.turn(Journey(email="a@b.co"), "hi")
    p = out["profile"]
    assert p["first_name"] == "Aisuluu" and p["known_customer"] is True
    assert p["source"] == ["bloomreach"] and p["retention_offer"] is None
    assert "recommended_skus" not in p                                     # panel gets a summary, not raw data


def test_profile_absent_when_not_loaded_or_disabled(mocks):
    out = make([[types.Part(text="Which room?")]]).turn(Journey(), "hi")
    assert out["profile"] is None
    off = make([[fc("get_customer_profile")], [types.Part(text="Hi")]], Settings(expose_profile=False))
    assert off.turn(Journey(email="a@b.co"), "hi")["profile"] is None


def test_demo_shopper_profile_drives_tier_and_offer(mocks):
    s = Settings(retention_discount_code="STAY15", churn_risk_threshold=0.6)
    agent = make([[fc("get_customer_profile")], [types.Part(text="Welcome back, Lashawna.")]], s)
    j = Journey(persona="lashawna")
    out = agent.turn(j, "I need to redo my living room")
    p = out["profile"]
    assert p["first_name"] == "Lashawna" and p["value_tier"] == "high" and p["persona"] == "lashawna"
    assert p["retention_offer"]["code"] == "STAY15" and p["source"] == ["databricks:snapshot"]
    elene = Toolbox(Journey(persona="elene"), agent.shop, agent.br, agent.features, s).get_customer_profile()
    assert elene["value_tier"] == "high" and elene["retention_offer"] is None       # churn 0.01: no code
    neal = Toolbox(Journey(persona="neal"), agent.shop, agent.br, agent.features, s).get_customer_profile()
    assert neal["value_tier"] == "mid" and neal["retention_offer"]["code"] == "STAY15"


def test_shopper_is_fixed_per_conversation(monkeypatch, mocks):
    from fastapi.testclient import TestClient
    from app import main
    agent = make([[types.Part(text="Hi")], [types.Part(text="Sure")], [types.Part(text="Hello")]])
    store = MemoryJourneyStore()
    monkeypatch.setattr(main, "get_agent", lambda: agent)
    monkeypatch.setattr(main, "get_store", lambda: store)
    c = TestClient(main.app)
    r1 = c.post("/api/chat", json={"message": "hi", "shopper": "neal"}).json()
    c.post("/api/chat", json={"message": "x", "session_id": r1["session_id"], "shopper": "elene"})
    assert store.load(r1["session_id"]).persona == "neal"
    r3 = c.post("/api/chat", json={"message": "hi", "shopper": "nobody"}).json()
    assert store.load(r3["session_id"]).persona is None


DBX = "https://dbc-test.cloud.databricks.com/api/2.0/sql/statements"


def test_databricks_customer_profile_lookup(mocks):
    from app.features import DatabricksFeatures
    seen = {}
    def handler(request):
        seen.update(json.loads(request.content))
        cols = ["customer_id", "first_name", "predicted_ltv", "avg_order_value", "churn_risk",
                "purchase_propensity_score", "loyalty_tier", "segment", "marketing_opt_in", "purchases_90d",
                "days_since_last_purchase", "value_tier", "offer_eligible"]
        row = ["CUST-0043", "Neal", "5850.0", "585.0", "0.669", "0.24", "silver", "at_risk", "true", "2", "140", "mid", "true"]
        return httpx.Response(200, json={"status": {"state": "SUCCEEDED"},
                                         "manifest": {"schema": {"columns": [{"name": c} for c in cols]}},
                                         "result": {"data_array": [row]}})
    mocks["router"].post(DBX).mock(side_effect=handler)
    br = Bloomreach(BR, "tok", "kid", "sec", "rec")
    f = DatabricksFeatures("dbc-test.cloud.databricks.com", "dapi", "wh1",
                           "`databricks-hackathon`.fourcast.customer_profile", BloomreachFeatures(br))
    p = f.get("Neal.Hawkins.0043@example.test")
    assert seen["parameters"] == [{"name": "email", "value": "neal.hawkins.0043@example.test"}]
    assert "`databricks-hackathon`.fourcast.customer_profile" in seen["statement"] and seen["warehouse_id"] == "wh1"
    assert p["predicted_ltv"] == 5850.0 and p["churn_risk"] == 0.669 and p["propensity"] == 0.24
    assert p["value_tier"] == "mid" and p["orders_count"] == 2 and "databricks" in p["source"]

    s = Settings(retention_discount_code="STAY15")
    tb = Toolbox(Journey(persona="neal", email="neal.hawkins.0043@example.test"),
                 ShopifyStorefront(SHOP, s.ucp_agent_profile), br, f, s)
    prof = tb.get_customer_profile()
    assert prof["source"] == ["bloomreach", "databricks"] and prof["retention_offer"]["code"] == "STAY15"
    assert prof["guidance"].startswith("Mid value")


def test_databricks_failure_falls_back(mocks):
    from app.features import DatabricksFeatures
    mocks["router"].post(DBX).mock(return_value=httpx.Response(200, json={"status": {"state": "FAILED", "error": {"message": "no table"}}}))
    br = Bloomreach(BR, "tok", "kid", "sec", "rec")
    f = DatabricksFeatures("dbc-test.cloud.databricks.com", "dapi", "wh1", "t", BloomreachFeatures(br))
    p = f.get("x@y.co")
    assert "databricks" not in p["source"] and p.get("predicted_ltv") is None


def test_demo_shopper_gets_their_databricks_email(monkeypatch, mocks):
    from fastapi.testclient import TestClient
    from app import main
    agent = make([[types.Part(text="Hi")]])
    store = MemoryJourneyStore()
    monkeypatch.setattr(main, "get_agent", lambda: agent)
    monkeypatch.setattr(main, "get_store", lambda: store)
    r = TestClient(main.app).post("/api/chat", json={"message": "hi", "shopper": "lashawna"}).json()
    assert r["email"] == "lashawna.cunningham.0036@example.test"


def test_rate_limited_lookup_waits_then_retries(mocks, monkeypatch):
    monkeypatch.setattr("app.shopify.time.sleep", lambda s: None)
    first = []
    def limited_once(request):
        if not first:
            first.append(1)
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": "Rate limit exceeded"})
        return ucp_response(request)
    mocks["router"].post(UCP).mock(side_effect=limited_once)
    agent = make([])
    out = Toolbox(Journey(), agent.shop, agent.br, agent.features, agent.s).find_products("sofa")
    assert out["products"] and "error" not in out


def test_repeat_lookups_are_served_from_cache(mocks):
    agent = make([])
    route = mocks["router"].post(UCP).mock(side_effect=ucp_response)
    agent.shop.lookup_variants(["51799233265700"])
    agent.shop.lookup_variants(["51799233265700"])
    assert route.call_count == 1


def test_later_search_is_not_marked_failed_by_an_earlier_one(mocks):
    calls = []
    def fail_first_two(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) <= 2 else ucp_response(request)
    mocks["router"].post(UCP).mock(side_effect=fail_first_two)
    agent = make([])
    t = Toolbox(Journey(), agent.shop, agent.br, agent.features, agent.s)
    assert "error" in t.find_products("sofa")
    later = t.find_products("rug")
    assert later["products"] and "error" not in later
