#!/usr/bin/env bash
# Deploy the T6 discovery agent (Gemini on Vertex AI) to Cloud Run.
#   ./scripts/deploy.sh            (GEMINI_MODEL defaults to gemini-3-flash-preview)
# Reuses the Bloomreach secrets created for t6-order-webhook.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-t6-discovery-agent}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3-flash-preview}"
GEMINI_LOCATION="${GEMINI_LOCATION:-global}"
SA="${SERVICE}-sa@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID" >/dev/null
echo "==> APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com firestore.googleapis.com secretmanager.googleapis.com

echo "==> Service account (Vertex AI user, Firestore user, Bloomreach secrets)"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "${SERVICE}-sa" --display-name="T6 discovery agent"
for role in roles/aiplatform.user roles/datastore.user roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role="$role" --condition=None >/dev/null
done
for s in t6-bloomreach-api-key-id t6-bloomreach-api-secret; do
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done

echo "==> Build & deploy"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --service-account "$SA" \
  --allow-unauthenticated --min-instances 1 --max-instances 5 --concurrency 20 \
  --cpu 1 --memory 1Gi --timeout 60 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$GEMINI_LOCATION,GEMINI_MODEL=$GEMINI_MODEL,SHOP_DOMAIN=team-fourcast.myshopify.com,BLOOMREACH_PROJECT_TOKEN=1e8016b8-b5e4-11f1-8438-ea6153e4edbf,BLOOMREACH_BASE_URL=https://api-engagement.bloomreach.com,STATE_BACKEND=firestore,FEATURES_BACKEND=bloomreach" \
  --set-secrets "BLOOMREACH_API_KEY_ID=t6-bloomreach-api-key-id:latest,BLOOMREACH_API_SECRET=t6-bloomreach-api-secret:latest"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo
echo "Agent chat UI: $URL"
echo "Smoke test:"
echo "  curl -s -X POST $URL/api/chat -H 'Content-Type: application/json' -d '{\"message\":\"I need to redecorate my living room\"}'"
