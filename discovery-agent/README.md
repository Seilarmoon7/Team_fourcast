# T6 discovery agent (Gemini × Bloomreach × Shopify, on Cloud Run)

The conversational entry point of the Full Journey Orchestrator. A customer says
"I need to redecorate my living room"; Gemini works out room, style and budget, recommends
in-stock products and hands off to Shopify checkout. The order webhook
(`../order-webhook`) closes the loop into Bloomreach, which runs the post-purchase journey.

```
browser ──/api/chat──▶ Cloud Run: discovery agent ──▶ Gemini (Vertex AI): reasons, picks tools
                             │  tools
                             ├─ get_customer_profile ─▶ Bloomreach profile + "T6 Personalized Picks"
                             │                          (+ Databricks features when FEATURES_BACKEND=databricks)
                             ├─ find_products ───────▶ catalog attributes (Bloomreach catalog export)
                             │                          + live price/stock from Shopify UCP lookup_catalog
                             ├─ get_personal_picks ──▶ Bloomreach recommendations → Shopify live check
                             ├─ save_preferences ────▶ Bloomreach customer properties
                             └─ create_checkout ─────▶ Shopify cart permalink (email, t6_session, discount)
             journey state ─▶ Firestore (Databricks-ready interface)
             every product shown ─▶ Bloomreach view_item / search / cart_update / checkout events
```

## Why each system is load-bearing

| Remove | What breaks |
|---|---|
| Gemini | No conversation: nothing turns "redecorate my living room" into room/style/budget and a shortlist |
| Bloomreach | No personal picks, no saved preferences, no post-purchase journey; recs stay random without the events this agent tracks |
| Shopify | No live stock/price and no checkout: the journey never becomes revenue |
| Databricks (next) | No LTV / churn-risk grounding, so no per-customer offer decision |

## Agent-to-agent (Pattern 1)

The agent supplies the intelligence and Bloomreach supplies the execution. When a shopper leaves
without buying, Gemini writes the campaign goal, subject, headline and message itself and calls
`request_campaign`; that becomes a `campaign_request` event, and the Bloomreach scenario
"T6 Agent-requested campaign" renders and sends it under Bloomreach's consent and frequency rules.
Guardrails in code: a known email, at least one product actually shown, one request per
conversation, HTML stripped and lengths capped.

## Agency (decisions the agent makes per customer)
- Which questions to ask (skips ones the profile already answers).
- Which products to show: style match, budget, Bloomreach recommendation boost, not already shown, in stock.
- Whether to offer a retention discount: only if churn risk ≥ `CHURN_RISK_THRESHOLD` and
  `RETENTION_DISCOUNT_CODE` is set. This is enforced in code, not left to the model.

## Chat UI (`app/static/index.html`)
One agent, one screen (Shae Smith's interface concept wired to the live agent):
- **Shopper picker**: Guest, or one of four demo customers (Lashawna, Elene, Neal, Rozanne) whose
  profiles mirror `databricks-hackathon.fourcast.customer_profile` (`app/personas.json`). The same
  Gemini agent reads the profile and changes its pitch: value tier (predicted LTV ≥ 8,000 → premium
  first, otherwise budget-led) and retention offer (churn ≥ `CHURN_RISK_THRESHOLD` and
  `RETENTION_DISCOUNT_CODE` set) are enforced in code, not by the model.
- **Behind the scenes** panel: which platform each tool call touched, the profile, tool trace,
  journey stage and the Bloomreach post-purchase journey.
- **Simulate payment** (after a real checkout link): plays the order-paid → Bloomreach nurture
  sequence for the demo; the real path is the Shopify webhook (`../order-webhook`).
- In the Shopify chat widget the panel is a drawer; "Full view" opens the full page.
  `/?shopper=neal` preselects a shopper; `/?rail=0` hides the panel.

`/api/chat` accepts an optional `shopper` key (fixed for the conversation) and returns a `profile`
summary for the panel. Set `EXPOSE_PROFILE=false` to stop returning it before real shoppers use it.

## Deploy (Cloud Shell)
```bash
./scripts/deploy.sh      # gemini-3-flash-preview; override with GEMINI_MODEL=gemini-2.5-flash
```
The Bloomreach private API key needs **Get** on customer properties and recommendations
(plus the event **Set** permissions the webhook already uses).

## Test
```bash
pip install -r requirements-dev.txt && pytest -q    # 7 tests, Gemini scripted, HTTP mocked
```
