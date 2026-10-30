#!/usr/bin/env bash
# Delete everything create_resources.sh made. Irreversible: BigQuery tables and
# uploaded files are destroyed.
#
#   export PROJECT_ID=my-project
#   ./deployment/teardown.sh
set -uo pipefail   # no -e: keep going if something is already gone
cd "$(dirname "$0")/.."
source deployment/config.sh

read -r -p "Delete ALL platform resources and data in project '$PROJECT_ID'? Type the project id: " ans
[[ "$ans" == "$PROJECT_ID" ]] || { echo "Aborted."; exit 1; }

gcloud config set project "$PROJECT_ID"
gcloud run services delete "$SERVICE_NAME" --region="$REGION" --quiet
gcloud functions delete "$FUNCTION_NAME" --gen2 --region="$REGION" --quiet   # also removes its Eventarc trigger + subscription
gcloud storage rm --recursive "gs://$BUCKET_NAME"   # objects, bucket and its Pub/Sub notification
bq rm -r -f -d "$PROJECT_ID:$BIGQUERY_DATASET"
gcloud pubsub topics delete "$TOPIC" --quiet
gcloud iam service-accounts delete "$WEBAPP_SA" --quiet
gcloud iam service-accounts delete "$INGEST_SA" --quiet
# Container images built by `--source` deploys
gcloud artifacts repositories delete cloud-run-source-deploy --location="$REGION" --quiet
gcloud artifacts repositories delete gcf-artifacts --location="$REGION" --quiet

echo "Teardown complete. APIs are left enabled (they cost nothing on their own)."
