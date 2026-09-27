"""The discovery agent: Gemini reasons, tools act.

Gemini decides what to ask, which tools to call and what to recommend; every product, price and
link it quotes comes from a tool result (Bloomreach catalog + recommendations, Shopify live
inventory/checkout). Business rules that must not depend on the model (who gets a discount,
which items are purchasable) are enforced inside the tools.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from google.genai import types

from .bloomreach import Bloomreach
from .shopify import ShopifyStorefront
from .state import Journey

log = logging.getLogger("discovery-agent")
CATALOG: list[dict] = json.loads((Path(__file__).parent / "catalog.json").read_text())
BY_SKU = {c["item_id"]: c for c in CATALOG}
PERSONAS: dict[str, dict] = {k: v for k, v in json.loads((Path(__file__).parent / "personas.json").read_text()).items()
                             if not k.startswith("_")}
HIGH_VALUE_LTV = 8000.0   # value-tier cutoff on predicted LTV


def value_tier(p: dict) -> dict:
    """How the agent should treat this customer. A policy in code, not left to the model."""
    ltv = p.get("predicted_ltv")
    if ltv is None and not p.get("value_tier"):
        return {}
    tier = p.get("value_tier") or ("high" if float(ltv) >= HIGH_VALUE_LTV else "mid")   # Databricks decides if it can
    if tier == "high":
        return {"value_tier": "high", "guidance": "High value: open with personal premium picks, "
                "skip budget questions unless they bring it up."}
    return {"value_tier": "mid", "guidance": "Mid value: ask for or respect a budget and show the "
            "cheapest good options first."}

SYSTEM_PROMPT = """You are the Nordhem Home stylist, a shopping assistant for a home-decor store.
Your job: turn a vague need ("I need to redecorate my living room") into a short, confident
shortlist the customer can buy right here, then hand off to checkout.

How to work:
- On the first turn call get_customer_profile. Use what it returns (saved style, budget, history,
  recommendations, lifetime value, churn risk) to skip questions you already know the answer to.
  Greet a known customer by first name. Follow the profile's `guidance` (value tier) for how to
  pitch: premium-first for high value, budget-led for mid value.
- Find out room, style and budget. Ask at most two short questions per reply, and offer 2-3
  concrete options ("warm Scandinavian, relaxed boho, or clean minimalist?") rather than open forms.
  If the customer gives enough to start, start; don't interrogate. If they name a piece ("a rug"),
  search and show options in that same reply, then refine from what they react to.
- When you learn a preference, call save_preferences.
- Search efficiently: one find_products call per piece type, at most 3 searches per reply. If a
  search is thin, show what you have and say what you could look for next, instead of searching again.
- Only recommend products returned by find_products or get_personal_picks. Never invent products,
  prices, stock or links. Present 3-4 picks max, each with one line on why it fits them, and the
  price. If a budget was given, keep the total within it and say so.
- If the profile says a retention offer is available, you may mention it once when the customer
  is deciding; never promise a discount the profile didn't offer.
- If they are leaving without buying, or want to think it over, offer to send the shortlist by email.
  If they agree and you have their email, call request_campaign once, writing the goal and the words
  yourself. Bloomreach owns consent, timing and delivery, so never promise when it will arrive.
- When the customer wants to buy, you need their email: ask for it if unknown. Then call
  create_checkout with exactly the items they want in THIS purchase: the ones named in their latest
  message. Never carry over items from an earlier checkout unless they ask for them again. Every
  checkout request needs a fresh create_checkout call; never reuse an old link. If unclear which
  items they mean, ask before creating the link.
- Keep replies short (under 120 words), warm and specific. No markdown tables."""


def _fd(name: str, description: str, props: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "parameters_json_schema": {"type": "object", "properties": props, "required": required or []}}


TOOL_DECLS = [
    _fd("get_customer_profile",
        "Load what we know about this customer: name, saved style/budget, lifetime value, churn risk, "
        "personalized recommendations and whether a retention offer is available.", {}),
    _fd("save_preferences", "Remember the customer's stated preferences for this and future conversations.", {
        "room": {"type": "string", "description": "living room, bedroom, dining room, home office or garden"},
        "style": {"type": "string", "description": "e.g. scandinavian, boho, minimalist, modern, mid-century, "
                                                   "traditional, industrial, coastal"},
        "budget_max": {"type": "number", "description": "Total budget in USD"},
        "pieces": {"type": "array", "items": {"type": "string"}, "description": "Pieces they want, e.g. sofa, rug"},
        "colors_avoid": {"type": "array", "items": {"type": "string"}},
        "email": {"type": "string"}}),
    _fd("find_products",
        "Search the live catalog. Returns in-stock products with live Shopify price and SKU.", {
            "keywords": {"type": "string", "description": "What to look for, e.g. 'sofa', 'desk', 'dining chair', "
                         "'rug', 'floor lamp' (5,000+ items across 43 categories)"},
            "room": {"type": "string"}, "style": {"type": "string"},
            "max_price": {"type": "number", "description": "Max price per item in USD"},
            "limit": {"type": "integer", "description": "1-6, default 4"}}),
    _fd("get_personal_picks",
        "Bloomreach personalized recommendations for this customer (in stock, live price).", {
            "limit": {"type": "integer", "description": "1-6, default 4"}}),
    _fd("request_campaign",
        "Hand Bloomreach a follow-up campaign for this customer, in your own words. Use it when the "
        "customer is leaving without buying, wants to think it over, or asks to be reminded, and they "
        "are happy to hear from us by email. Bloomreach decides consent, timing and sending; you supply "
        "the goal, the products and the words. One per conversation.", {
            "goal": {"type": "string", "description": "The campaign goal in plain language, e.g. 'bring "
                     "Aisuluu back to finish the Scandinavian living room she priced at $1,200'"},
            "subject": {"type": "string", "description": "Email subject line"},
            "headline": {"type": "string", "description": "Headline inside the email"},
            "message": {"type": "string", "description": "One or two sentences, addressed to the customer"},
            "cta_label": {"type": "string", "description": "Button text, e.g. 'Pick up where we left off'"},
            "skus": {"type": "array", "items": {"type": "string"},
                     "description": "The exact SKUs to feature, chosen from the ones you showed them in "
                                    "this conversation. Pick only products that match what they asked for."}},
        ["goal", "subject", "headline", "message", "skus"]),
    _fd("create_checkout", "Create the Shopify checkout link for the chosen items.", {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "sku": {"type": "string"}, "quantity": {"type": "integer"}}, "required": ["sku"]}},
        "email": {"type": "string"}}, ["items"]),
]

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Toolbox:
    """Executes tool calls against real systems, and records what the UI should render."""

    def __init__(self, journey: Journey, shop: ShopifyStorefront, br: Bloomreach, features, settings) -> None:
        self.j, self.shop, self.br, self.features, self.s = journey, shop, br, features, settings
        self.ui_products: list[dict] = []
        self._profile: dict | None = None
        self.shopify_down = False
        self.new_checkout_url: str | None = None

    # -- helpers ---------------------------------------------------------------------------------
    def _profile_data(self) -> dict:
        if self._profile is None:
            persona = PERSONAS.get(self.j.persona or "")
            self._profile = self.features.get(self.j.email)
            if persona and "databricks" not in self._profile.get("source", []):
                # Databricks not configured/reachable: use the offline snapshot of the same row
                snap = {k: v for k, v in persona.items() if k != "email"}
                self._profile = {**self._profile, "known_customer": True, **snap,
                                 "source": self._profile.get("source", []) + ["databricks:snapshot"]}
            if persona and persona.get("history"):
                self._profile.setdefault("history", persona["history"])
            self._profile.update(value_tier(self._profile))
        return self._profile

    def _offer(self) -> dict | None:
        p = self._profile_data()
        risk = p.get("churn_risk")
        if self.s.retention_discount_code and risk is not None and risk >= self.s.churn_risk_threshold:
            return {"code": self.s.retention_discount_code, "reason": "retention"}
        return None

    def _track(self, events: list[tuple[str, dict]], properties: dict | None = None) -> None:
        try:
            self.br.track(self.j.email or "", events, properties)
        except Exception as e:
            log.warning("bloomreach_track_failed %s", e)

    def _live(self, items: list[dict], limit: int) -> list[dict]:
        """Attach live Shopify price/stock; keep only purchasable items."""
        ids = [c["variant_id"] for c in items[: limit * 3]]
        self.shopify_down = False   # reflects this lookup only, not an earlier one this turn
        live = None
        for attempt in (1, 2):
            try:
                live = self.shop.lookup_variants(ids)
                break
            except Exception as e:
                log.warning("shopify_lookup_failed attempt=%s %s", attempt, e)
        if live is None:
            self.shopify_down = True
            return []
        out = []
        for c in items:
            lv = live.get(c["variant_id"])
            if not lv or not lv["available"]:
                continue
            out.append({"sku": c["item_id"], "title": c["title"], "price": lv["price"], "style": c["style"],
                        "room": c["room"], "color": c["color"], "material": c["material"]})
            if len(out) >= limit:
                break
        if out:
            self.ui_products.extend(out)
            for p in out:
                if p["sku"] not in self.j.shown:
                    self.j.shown.append(p["sku"])
            # view_item feeds the Bloomreach recommendation engine -> recs get personal over time
            self._track([("view_item", {"product_id": p["sku"], "title": p["title"], "price": p["price"],
                                        "tags": [p["style"], p["room"]], "category_level_1": p["room"],
                                        "source": "discovery_agent"}) for p in out])
        return out

    def ui_profile(self) -> dict | None:
        """What the 'behind the scenes' panel shows. Only if a tool loaded the profile this turn."""
        if self._profile is None:
            return None
        p, offer = self._profile, self._offer()
        keys = ("first_name", "known_customer", "predicted_ltv", "avg_order_value", "churn_risk",
                "orders_count", "style_preference", "budget_max", "value_tier", "guidance",
                "loyalty_tier", "segment", "propensity", "customer_id", "days_since_last_purchase")
        return {**{k: p.get(k) for k in keys}, "persona": self.j.persona, "source": list(p.get("source") or []),
                "retention_offer": offer, "churn_risk_threshold": self.s.churn_risk_threshold}

    # -- tools -----------------------------------------------------------------------------------
    def get_customer_profile(self) -> dict:
        p = dict(self._profile_data())
        p["saved_preferences"] = self.j.prefs
        p["retention_offer"] = self._offer()
        p["recommended_skus"] = p.get("recommended_skus", [])[:6]
        return p

    def save_preferences(self, **kw) -> dict:
        email = kw.pop("email", None)
        if email and _EMAIL.match(email.strip()):
            self.j.email = email.strip().lower()
            self._profile = None
        self.j.prefs.update({k: v for k, v in kw.items() if v not in (None, "", [])})
        props = {"style_preference": self.j.prefs.get("style"), "budget_max": self.j.prefs.get("budget_max"),
                 "room_focus": self.j.prefs.get("room")}
        self._track([], {k: v for k, v in props.items() if v is not None})
        return {"saved": self.j.prefs, "email": self.j.email}

    def find_products(self, keywords: str = "", room: str | None = None, style: str | None = None,
                      max_price: float | None = None, limit: int = 4) -> dict:
        limit = max(1, min(int(limit or 4), 6))
        room = (room or self.j.prefs.get("room") or "").lower() or None
        style = (style or self.j.prefs.get("style") or "").lower() or None
        words = [w for w in re.findall(r"[a-z]+", (keywords or "").lower()) if len(w) > 2]
        recs = set(self._profile_data().get("recommended_skus", []))
        scored = []
        for c in CATALOG:
            if max_price and c["price"] > max_price:
                continue
            if room and c["room"] != room:
                continue
            hay = " ".join([c["title"], c.get("description") or "", " ".join(c.get("tags") or []),
                            c.get("category_level_2") or ""]).lower()
            hits = sum(1 for w in words if w in hay or w.rstrip("s") in hay)
            if words and hits == 0:
                continue
            score = hits * 2 + (3 if style and c["style"] == style else 0) + (1.5 if c["item_id"] in recs else 0)
            score += 1.0 if c["item_id"].startswith("lr-") else 0   # curated range over legacy sandbox items
            score -= 0.5 if c["item_id"] in self.j.shown else 0
            scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        products = self._live([c for _, c in scored], limit)
        if self.j.stage == "discovery" and products:
            self.j.stage = "recommending"
        if keywords:
            self._track([("search", {"search_term": keywords, "source": "discovery_agent"})])
        out = {"products": products, "matched": len(scored)}
        if self.shopify_down:
            out["error"] = "store_unavailable: live stock/price lookup failed, try again once"
        elif not products:
            out["hint"] = "nothing in stock matched; relax one filter (style or max_price) or use broader keywords"
        return out

    def get_personal_picks(self, limit: int = 4) -> dict:
        limit = max(1, min(int(limit or 4), 6))
        skus = self._profile_data().get("recommended_skus", [])
        items = [BY_SKU[s] for s in skus if s in BY_SKU]
        return {"products": self._live(items, limit), "source": "bloomreach:T6 Personalized Picks"}

    def request_campaign(self, goal: str, subject: str, headline: str, message: str,
                         cta_label: str = "Pick up where we left off", skus: list | None = None) -> dict:
        """Agent-to-agent handoff: we state the goal, Bloomreach runs the campaign."""
        if not self.j.email:
            return {"error": "need_email", "message": "Ask the customer for their email first."}
        if self.j.campaign_requested:
            return {"error": "already_requested", "goal": self.j.campaign_requested}
        # only products actually shown in this conversation, and only ones the model named
        picked = [s for s in (skus or []) if s in BY_SKU and s in self.j.shown]
        chosen = [BY_SKU[s] for s in (picked or self.j.shown[-4:]) if s in BY_SKU][:4]
        if not chosen:
            return {"error": "no_products", "message": "Show them some products first."}
        live = {}
        try:
            live = self.shop.lookup_variants([c["variant_id"] for c in chosen])
        except Exception as e:
            log.warning("shopify_lookup_failed %s", e)
        products = [{"sku": c["item_id"], "title": c["title"],
                     "price": live.get(c["variant_id"], {}).get("price", c["price"])} for c in chosen]
        clip = lambda t, n: re.sub(r"<[^>]+>", "", str(t or ""))[:n]
        props = {
            "channel": "email", "requested_by": "discovery_agent", "session_id": self.j.session_id,
            "goal": clip(goal, 400), "subject": clip(subject, 120), "headline": clip(headline, 120),
            "message": clip(message, 400), "cta_label": clip(cta_label, 40),
            "cta_url": f"https://{self.s.shop_domain}",
            "products": products, "product_ids": [p["sku"] for p in products],
            "room": self.j.prefs.get("room"), "style": self.j.prefs.get("style"),
            "budget_max": self.j.prefs.get("budget_max"),
        }
        try:
            self.br.track(self.j.email, [("campaign_request", props)])
        except Exception as e:
            log.warning("campaign_request_failed %s", e)
            return {"error": "handoff_failed", "message": "Bloomreach did not accept the request."}
        self.j.campaign_requested = props["goal"]
        return {"handed_to": "bloomreach", "goal": props["goal"], "products": [p["sku"] for p in products],
                "note": "Bloomreach applies consent and frequency rules before sending."}

    def create_checkout(self, items: list[dict], email: str | None = None) -> dict:
        if email and _EMAIL.match(email.strip()):
            self.j.email = email.strip().lower()
            self._profile = None
        if not self.j.email:
            return {"error": "need_email", "message": "Ask the customer for their email first."}
        lines, unknown = [], []
        for it in items or []:
            c = BY_SKU.get(str(it.get("sku")))
            if not c:
                unknown.append(it.get("sku"))
                continue
            lines.append((c, max(1, int(it.get("quantity") or 1))))
        if not lines:
            return {"error": "no_valid_items", "unknown_skus": unknown}
        live = self.shop.lookup_variants([c["variant_id"] for c, _ in lines])
        sold_out = [c["item_id"] for c, _ in lines if not live.get(c["variant_id"], {}).get("available")]
        lines = [(c, q) for c, q in lines if c["item_id"] not in sold_out]
        if not lines:
            return {"error": "sold_out", "skus": sold_out}
        offer = self._offer()
        url = self.shop.checkout_url([(c["variant_id"], q) for c, q in lines], email=self.j.email,
                                     discount=offer["code"] if offer else None,
                                     attributes={"t6_session": self.j.session_id})
        total = round(sum(live[c["variant_id"]]["price"] * q for c, q in lines), 2)
        self.j.checkout_url, self.j.stage = url, "checkout"
        self.new_checkout_url = url
        self._track([("cart_update", {"action": "add", "product_id": c["item_id"], "title": c["title"],
                                      "quantity": q, "source": "discovery_agent"}) for c, q in lines]
                    + [("checkout", {"step_number": 1, "step_title": "agent_handoff", "total_price": total,
                                     "product_ids": [c["item_id"] for c, _ in lines],
                                     "voucher_code": offer["code"] if offer else None,
                                     "session_id": self.j.session_id})])
        return {"checkout_url": url, "total_before_discount": total, "discount_code": offer["code"] if offer else None,
                "items": [{"sku": c["item_id"], "title": c["title"], "quantity": q} for c, q in lines],
                "skipped_sold_out": sold_out, "unknown_skus": unknown}

    def run(self, name: str, args: dict) -> dict:
        fn = getattr(self, name, None)
        if name not in {d["name"] for d in TOOL_DECLS} or fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return {"error": f"bad arguments: {e}"}
        except Exception as e:
            log.exception("tool_failed %s", name)
            return {"error": f"{name} failed: {type(e).__name__}"}


class DiscoveryAgent:
    def __init__(self, client, settings, shop: ShopifyStorefront, br: Bloomreach, features) -> None:
        self.client, self.s, self.shop, self.br, self.features = client, settings, shop, br, features
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(**d) for d in TOOL_DECLS])],
            # Gemini 3 is tuned for temperature 1.0 (lower values can cause looping); keep 0.6 for 2.x
            temperature=None if settings.gemini_model.startswith("gemini-3") else 0.6,
            # shopping chat needs quick turns, not deep reasoning
            thinking_config=types.ThinkingConfig(thinking_level="LOW")
            if settings.gemini_model.startswith("gemini-3") else None,
        )

    def turn(self, journey: Journey, message: str) -> dict:
        if journey.stage == "checkout":
            journey.stage = "recommending"
        tools = Toolbox(journey, self.shop, self.br, self.features, self.s)
        contents = [types.Content.model_validate(h) for h in journey.history]
        contents.append(types.Content(role="user", parts=[types.Part(text=message)]))
        reply, trace = "", []
        for _ in range(self.s.max_tool_rounds):
            resp = self.client.models.generate_content(model=self.s.gemini_model, contents=contents,
                                                       config=self.config)
            cand = resp.candidates[0].content if resp.candidates else None
            if cand is None:
                break
            contents.append(cand)
            calls = [p.function_call for p in (cand.parts or []) if p.function_call]
            if not calls:
                reply = "".join(p.text for p in (cand.parts or []) if p.text and not p.thought).strip()
                break
            parts = []
            for fc in calls:
                result = tools.run(fc.name, dict(fc.args or {}))
                trace.append({"tool": fc.name, "args": dict(fc.args or {}),
                              "ok": "error" not in result, "n": len(result.get("products", []))
                              if isinstance(result.get("products"), list) else None})
                parts.append(types.Part.from_function_response(name=fc.name, response={"result": result}))
            contents.append(types.Content(role="user", parts=parts))
        if not reply:  # tool budget used up: force a text answer from what the tools returned
            try:
                final_cfg = self.config.model_copy(update={"tool_config": types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="NONE"))})
                resp = self.client.models.generate_content(model=self.s.gemini_model, contents=contents,
                                                           config=final_cfg)
                cand = resp.candidates[0].content if resp.candidates else None
                if cand and cand.parts:
                    reply = "".join(p.text for p in cand.parts if p.text and not p.thought).strip()
                    contents.append(cand)
            except Exception:
                log.exception("final_answer_failed")
        if not reply:
            reply = "Sorry, I lost my train of thought there. Could you say that again?"
        journey.history = [c.model_dump(mode="json", exclude_none=True) for c in contents]
        # cards = products shown this turn + any earlier pick the reply mentions by name
        cards = list(tools.ui_products)
        low = reply.lower()
        for sku in journey.shown:
            c = BY_SKU.get(sku)
            if c and c["title"].lower() in low:
                cards.append({"sku": sku, "title": c["title"], "price": c["price"], "style": c["style"],
                              "room": c["room"], "color": c["color"], "material": c["material"]})
        mentioned = [p for p in cards if p["title"].lower() in low]
        cards = mentioned or cards
        return {"reply": reply, "products": _dedupe(cards), "checkout_url": tools.new_checkout_url, "stage": journey.stage, "email": journey.email,
                "profile": tools.ui_profile() if self.s.expose_profile else None, "trace": trace}


def _dedupe(products: list[dict]) -> list[dict]:
    seen, out = set(), []
    for p in products:
        if p["sku"] not in seen:
            seen.add(p["sku"])
            out.append(p)
    return out
