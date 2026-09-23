#!/usr/bin/env bash
# Connect both Cloud Run services to Databricks (run in Cloud Shell, from ~):
#   bash enable_databricks.sh
# - discovery agent  : reads fourcast.customer_profile (LTV, churn, value tier) for every shopper
# - order webhook    : writes every paid Shopify order into fourcast.orders
# The token is read from the terminal (hidden) and stored in Secret Manager; it never goes in a file.
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
HOST="${DATABRICKS_HOST:-dbc-2124425a-10dd.cloud.databricks.com}"
WAREHOUSE="${DATABRICKS_WAREHOUSE_ID:-bb33046c9a986a2b}"
SECRET=t6-databricks-token

if ! gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
  read -rsp "Paste the Databricks personal access token (dapi...): " TOKEN; echo
  printf '%s' "$TOKEN" | gcloud secrets create "$SECRET" --data-file=- --replication-policy=automatic
  unset TOKEN
fi

for SVC in t6-discovery-agent t6-order-webhook; do
  if ! gcloud run services describe "$SVC" --region "$REGION" >/dev/null 2>&1; then
    echo "skip $SVC (not deployed in $PROJECT_ID)"; continue
  fi
  SA=$(gcloud run services describe "$SVC" --region "$REGION" --format='value(spec.template.spec.serviceAccountName)')
  [[ -z "$SA" ]] && SA="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
  gcloud secrets add-iam-policy-binding "$SECRET" --member="serviceAccount:$SA" \
    --role=roles/secretmanager.secretAccessor >/dev/null
  EXTRA="FEATURES_BACKEND=databricks"
  [[ "$SVC" == t6-order-webhook ]] && EXTRA="DATABRICKS_ORDERS_TABLE=\`databricks-hackathon\`.fourcast.orders"
  echo "==> $SVC (service account $SA)"
  gcloud run services update "$SVC" --region "$REGION" \
    --update-env-vars "^@^DATABRICKS_HOST=$HOST@DATABRICKS_WAREHOUSE_ID=$WAREHOUSE@DATABRICKS_FEATURES_TABLE=\`databricks-hackathon\`.fourcast.customer_profile@$EXTRA" \
    --update-secrets "DATABRICKS_TOKEN=$SECRET:latest"
done
echo "Done. Test: pick 'Neal' in the stylist and say hi; the Databricks light should turn on."
