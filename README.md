# Team Fourcast: T6 Full Journey Orchestrator

An AI shopping agent for a home-decor store that runs the whole customer journey across four platforms:

| Platform | Role |
|---|---|
| **Google Gemini** (Vertex AI) | Conversation and tool-calling: turns "I need to redo my living room" into room, style, budget and a shortlist |
| **Databricks** | Customer intelligence: predicted LTV, churn risk, value tier and offer eligibility from `fourcast.customer_profile`; paid orders written back to `fourcast.orders` |
| **Bloomreach Engagement** | Profiles, events (view_item, cart_update, checkout, purchase), personalized picks and the post-purchase journey |
| **Shopify** | Live price and stock, checkout, and the order webhook that closes the loop |

## Architecture & data flow

One reasoning agent (Gemini), one execution layer (`Toolbox`, in-process inside the discovery-agent
service, not a separate service), and four external systems it reaches into. Gemini never talks to
Shopify, Bloomreach, Databricks or Firestore directly — every call into those systems originates
from the agent, which is also where the discount/consent/purchasability rules run (never left up
to the model).

```mermaid
flowchart TB
    Browser["Browser<br/>shopper chat UI"]
    Agent["Cloud Run · Discovery Agent<br/>FastAPI /api/chat + Toolbox<br/>orchestrator — enforces discount,<br/>consent-gate & purchasability rules"]
    Gemini["Gemini · Vertex AI<br/>reasoning + tool selection only<br/>no direct data access"]
    Firestore[("Firestore<br/>journeys collection")]
    Shopify["Shopify UCP<br/>live price · stock · checkout"]
    Bloomreach["Bloomreach Engagement<br/>profile · recs · events<br/>campaign hand-off"]
    Databricks[("Databricks SQL<br/>customer_profile feature table")]
    Webhook["Order Webhook<br/>separate Cloud Run service"]

    Browser -->|"POST /api/chat {message}"| Agent
    Agent -->|"reply + cards + checkout_url"| Browser
    Agent <-->|"generate_content(), up to 8 rounds"| Gemini
    Agent <-->|"load / save Journey"| Firestore
    Agent -->|"find_products, checkout — blocks the reply"| Shopify
    Agent -->|"profile read, once per conversation"| Bloomreach
    Agent -.->|"track(): view/search/cart/checkout — best-effort"| Bloomreach
    Agent -.->|"SELECT profile · falls back on failure"| Databricks
    Shopify -.->|"order-paid webhook, real purchase only"| Webhook
    Webhook -.->|"post-purchase journey trigger"| Bloomreach
```

**Solid arrows block the reply** (the customer waits on them); **dashed arrows are best-effort or
handled elsewhere** — telemetry writes swallow their own failures, the Databricks read is
feature-flagged and degrades silently, and the order-webhook loop that actually closes a sale into
Bloomreach's post-purchase journey runs in a separate Cloud Run service on Shopify's own schedule,
not this one.

For the full deep-dive — scale/concurrency analysis, retry and caching maps, failure-mode and
blast-radius diagrams, and the business-value math behind the discount logic — ask in the team
channel for the current handbook link.

## Design principles

**The model can't invent a product.** Every product card the shopper sees is built from a real
tool-call result — a live search against the catalog, cross-checked against Shopify for current
price and stock — never from parsing what Gemini wrote. The reply text and the product cards are
two independently-sourced outputs that happen to render together, so there's no path from "the
model said it" to "the customer sees a price for it." What's shown is always what a tool actually
returned.

**Retention logic is a business rule, not a model judgment call.** Whether a shopper is offered a
discount is decided in code from Databricks' churn signal, gated by a fixed threshold — the model
reads the outcome and can mention it, but never decides it. The same conversation, run for two
different customers, produces two different outcomes purely because of what the data says about
each of them — not because the model was prompted differently.

**A vague need becomes a coherent room, not a single lucky match.** When a shopper describes a
feeling rather than an item — "my living room feels cold" — the agent reasons about what actually
answers that (a rug, a lamp, a throw) and searches for each piece type on its own, then presents
them as one considered set. The shopper keeps that set as a running collection they can add to and
remove from across the rest of the conversation, and buy as one checkout when they're ready.

**One agent hands off to another, it doesn't just fire an API call.** When a shopper stalls without
buying, the discovery agent doesn't draft or send anything itself — it hands Bloomreach a goal
("bring her back to finish the home office setup"), and Bloomreach's own campaign engine decides
consent, timing and channel independently. Two systems, each deciding the part it's actually
responsible for.

**The loop closes on a real purchase, not a simulated one.** A paid Shopify order fires a webhook
that writes the sale back into Databricks' customer record and triggers Bloomreach's post-purchase
nurture — the same intelligence layer that shaped the original pitch gets updated by what the
shopper actually did, so the next conversation starts from more accurate signal than the last one.

## Repository layout

```
discovery-agent/   Cloud Run service: Gemini agent + chat UI (app/static/index.html)
order-webhook/     Cloud Run service: Shopify orders -> Bloomreach purchase events + Databricks orders table
bloomreach/        Scenario builder for the agent-requested campaign
stylist-ui/        Standalone copy of the chat UI
enable_databricks.sh  Connects both services to Databricks (token kept in Secret Manager)
```

## Run the tests

```bash
cd discovery-agent && pip install -r requirements-dev.txt && pytest -q
cd ../order-webhook && pip install -r requirements-dev.txt && pytest -q
```

## Deploy (Cloud Shell)

```bash
cd discovery-agent && ./scripts/deploy.sh
cd ../order-webhook && ./scripts/deploy.sh
cd .. && bash enable_databricks.sh
```

Product catalog: `discovery-agent/scripts/gen_catalog.py` generates the 5,000-item Shopify CSV;
`scripts/sync_catalog.py` refreshes the agent's catalog after an import.
