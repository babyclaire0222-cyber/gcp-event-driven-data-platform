#!/usr/bin/env bash
# Deploy code only (resources must exist; see create_resources.sh). Used by CD too.
set -euo pipefail
cd "$(dirname "$0")/.."
source deployment/config.sh

echo "==> Deploying Cloud Function '$FUNCTION_NAME' (Pub/Sub trigger on '$TOPIC')"
gcloud functions deploy "$FUNCTION_NAME" \
  --gen2 --runtime=python312 --region="$REGION" \
  --source=functions/ingest --entry-point=ingest \
  --trigger-topic="$TOPIC" --retry \
  --service-account="$INGEST_SA" --trigger-service-account="$INGEST_SA" \
  --memory=512Mi --timeout=300s --max-instances=5 \
  --set-env-vars="GCP_PROJECT=$PROJECT_ID,BIGQUERY_DATASET=$BIGQUERY_DATASET"

echo "==> Deploying Cloud Run service '$SERVICE_NAME' (IAP, no public access)"
gcloud beta run deploy "$SERVICE_NAME" \
  --source=webapp --region="$REGION" \
  --service-account="$WEBAPP_SA" \
  --no-allow-unauthenticated --iap \
  --memory=512Mi --max-instances=3 --concurrency=40 \
  --set-env-vars="GCP_PROJECT=$PROJECT_ID,BUCKET_NAME=$BUCKET_NAME,BIGQUERY_DATASET=$BIGQUERY_DATASET"
