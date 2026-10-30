#!/usr/bin/env bash
# One-time setup of every GCP resource, then a first deploy.
#
#   export PROJECT_ID=my-project IAP_USER=me@gmail.com
#   ./deployment/create_resources.sh
#
# Safe to re-run: "already exists" errors on create steps are ignored.
set -euo pipefail
cd "$(dirname "$0")/.."
source deployment/config.sh
: "${IAP_USER:?Set IAP_USER to the Google account allowed through IAP}"

gcloud config set project "$PROJECT_ID"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
exists_ok() { "$@" 2>&1 | grep -v -i "already exists" || true; }

echo "==> 1/6 Enabling APIs"
gcloud services enable \
  run.googleapis.com cloudfunctions.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com eventarc.googleapis.com pubsub.googleapis.com \
  storage.googleapis.com bigquery.googleapis.com iap.googleapis.com

echo "==> 2/6 Storage: bucket with 30-day cleanup of raw and rejected files"
exists_ok gcloud storage buckets create "gs://$BUCKET_NAME" \
  --location="$REGION" --uniform-bucket-level-access
cat > /tmp/lifecycle.json <<'JSON'
{"rule": [{"action": {"type": "Delete"},
           "condition": {"age": 30, "matchesPrefix": ["uploads/", "rejected/"]}}]}
JSON
gcloud storage buckets update "gs://$BUCKET_NAME" --lifecycle-file=/tmp/lifecycle.json

echo "==> 3/6 BigQuery dataset (same region as the bucket; cross-region loads fail)"
exists_ok bq --location="$REGION" mk --dataset "$PROJECT_ID:$BIGQUERY_DATASET"

echo "==> 4/6 Pub/Sub topic + GCS notification on uploads/"
exists_ok gcloud pubsub topics create "$TOPIC"
GCS_AGENT=$(gcloud storage service-agent --project="$PROJECT_ID")
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
  --member="serviceAccount:$GCS_AGENT" --role=roles/pubsub.publisher >/dev/null
if ! gcloud storage buckets notifications list "gs://$BUCKET_NAME" --format='value(topic)' | grep -q "$TOPIC"; then
  gcloud storage buckets notifications create "gs://$BUCKET_NAME" --topic="$TOPIC" \
    --event-types=OBJECT_FINALIZE --object-prefix=uploads/ --payload-format=json
fi

echo "==> 5/6 Service accounts (one per component, least privilege)"
exists_ok gcloud iam service-accounts create webapp-sa --display-name="Cloud Run webapp"
exists_ok gcloud iam service-accounts create ingest-sa --display-name="Ingest Cloud Function"

bind_project() {
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition=None \
    --member="serviceAccount:$1" --role="$2" >/dev/null
}
bind_bucket() {
  gcloud storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
    --member="serviceAccount:$1" --role="$2" >/dev/null
}
# webapp: write uploads, read rejection reasons, query and create views
bind_bucket  "$WEBAPP_SA" roles/storage.objectUser
bind_project "$WEBAPP_SA" roles/bigquery.dataEditor
bind_project "$WEBAPP_SA" roles/bigquery.jobUser
# ingest: read/move objects, run load jobs, receive Eventarc events
bind_bucket  "$INGEST_SA" roles/storage.objectUser
bind_project "$INGEST_SA" roles/bigquery.dataEditor
bind_project "$INGEST_SA" roles/bigquery.jobUser
bind_project "$INGEST_SA" roles/eventarc.eventReceiver
bind_project "$INGEST_SA" roles/run.invoker

echo "==> 6/6 Deploy function + Cloud Run, then open IAP to $IAP_USER"
./deployment/deploy.sh

gcloud beta services identity create --service=iap.googleapis.com >/dev/null
gcloud run services add-iam-policy-binding "$SERVICE_NAME" --region="$REGION" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" \
  --role=roles/run.invoker >/dev/null
gcloud beta iap web add-iam-policy-binding --resource-type=cloud-run \
  --service="$SERVICE_NAME" --region="$REGION" \
  --member="user:$IAP_USER" --role=roles/iap.httpsResourceAccessor >/dev/null

URL=$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" --format='value(status.url)')
echo
echo "Done. Open $URL and sign in as $IAP_USER."
