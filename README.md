# Team Fourcast: T6 Full Journey Orchestrator

An AI shopping agent for a home-decor store that runs the whole customer journey across four platforms:

| Platform | Role |
|---|---|
| **Google Gemini** (Vertex AI) | Conversation and tool-calling: turns "I need to redo my living room" into room, style, budget and a shortlist |
| **Databricks** | Customer intelligence: predicted LTV, churn risk, value tier and offer eligibility from `fourcast.customer_profile`; paid orders written back to `fourcast.orders` |
| **Bloomreach Engagement** | Profiles, events (view_item, cart_update, checkout, purchase), personalized picks and the post-purchase journey |
| **Shopify** | Live price and stock, checkout, and the order webhook that closes the loop |

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
