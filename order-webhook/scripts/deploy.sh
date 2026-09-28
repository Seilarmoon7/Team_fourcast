#!/usr/bin/env bash
# Deploy the T6 order webhook to Cloud Run.
# Prereqs: gcloud auth login; a GCP project with billing. Run from the repo root.
#   PROJECT_ID=qwiklabs-gcp-00-8078dcc7f11b ./scripts/deploy.sh
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-t6-order-webhook}"
SA="${SERVICE}-sa@${PROJECT_ID}.iam.gserviceaccount.com"
ALERT_EMAIL="${ALERT_EMAIL:-}"   # optional: where to send failure alerts

gcloud config set project "$PROJECT_ID" >/dev/null

echo "==> Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com firestore.googleapis.com monitoring.googleapis.com logging.googleapis.com

echo "==> Firestore (native mode) for idempotency keys"
gcloud firestore databases describe --database='(default)' >/dev/null 2>&1 || \
  gcloud firestore databases create --location="$REGION" --type=firestore-native

echo "==> Service account"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "${SERVICE}-sa" --display-name="T6 order webhook"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" \
  --role=roles/datastore.user --condition=None >/dev/null

echo "==> Secrets (you will be prompted for any that don't exist yet; input is hidden)"
for name in SHOPIFY_WEBHOOK_SECRET BLOOMREACH_API_KEY_ID BLOOMREACH_API_SECRET; do
  secret_id="t6-$(echo "$name" | tr '[:upper:]_' '[:lower:]-')"
  if ! gcloud secrets describe "$secret_id" >/dev/null 2>&1; then
    read -r -s -p "Value for $name: " val; echo
    printf '%s' "$val" | gcloud secrets create "$secret_id" --data-file=- --replication-policy=automatic
    unset val
  fi
  gcloud secrets add-iam-policy-binding "$secret_id" --member="serviceAccount:$SA" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done

echo "==> Build & deploy"
gcloud run deploy "$SERVICE" --source . --region "$REGION" \
  --service-account "$SA" \
  --allow-unauthenticated \
  --min-instances 1 --max-instances 5 --concurrency 20 --cpu 1 --memory 512Mi --timeout 10 \
  --set-env-vars "BLOOMREACH_PROJECT_TOKEN=1e8016b8-b5e4-11f1-8438-ea6153e4edbf,BLOOMREACH_BASE_URL=https://api-engagement.bloomreach.com,DEDUPE_BACKEND=firestore" \
  --set-secrets "SHOPIFY_WEBHOOK_SECRET=t6-shopify-webhook-secret:latest,BLOOMREACH_API_KEY_ID=t6-bloomreach-api-key-id:latest,BLOOMREACH_API_SECRET=t6-bloomreach-api-secret:latest"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')

echo "==> Log-based metric + alert on tracking failures"
gcloud logging metrics describe t6_bloomreach_track_failed >/dev/null 2>&1 || \
  gcloud logging metrics create t6_bloomreach_track_failed \
    --description="Shopify order could not be forwarded to Bloomreach" \
    --log-filter="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SERVICE\" AND jsonPayload.event=\"bloomreach_track_failed\""

if [[ -n "$ALERT_EMAIL" ]]; then
  CHANNEL=$(gcloud beta monitoring channels create --type=email --display-name="T6 on-call" \
    --channel-labels=email_address="$ALERT_EMAIL" --format='value(name)')
  cat > /tmp/t6-alert.json <<JSON
{
  "displayName": "T6: order not forwarded to Bloomreach",
  "combiner": "OR",
  "conditions": [{
    "displayName": "bloomreach_track_failed > 0",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/t6_bloomreach_track_failed\" AND resource.type=\"cloud_run_revision\"",
      "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s",
      "aggregations": [{"alignmentPeriod": "60s", "perSeriesAligner": "ALIGN_COUNT"}]
    }
  }],
  "notificationChannels": ["$CHANNEL"]
}
JSON
  gcloud alpha monitoring policies create --policy-from-file=/tmp/t6-alert.json
fi

echo
echo "Deployed: $URL"
echo "Shopify webhook URL (topics orders/paid, orders/updated, orders/cancelled):"
echo "  $URL/webhooks/shopify/orders"
echo
echo "==> IMPORTANT: this script's --set-env-vars REPLACES the whole env list, so it just reset"
echo "    this service's Databricks wiring (DATABRICKS_HOST/WAREHOUSE_ID/ORDERS_TABLE + token) if"
echo "    it was on before this deploy. Restore it now - safe to re-run, it only merges:"
echo "      cd .. && bash enable_databricks.sh"
