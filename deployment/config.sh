# Shared settings for create_resources.sh, deploy.sh and teardown.sh.
# Override any of these with environment variables.
: "${PROJECT_ID:?Set PROJECT_ID, e.g. export PROJECT_ID=my-project}"
REGION="${REGION:-asia-southeast1}"
BUCKET_NAME="${BUCKET_NAME:-${PROJECT_ID}-uploads}"
BIGQUERY_DATASET="${BIGQUERY_DATASET:-analysis_dataset}"
TOPIC="${TOPIC:-uploads}"
SERVICE_NAME="${SERVICE_NAME:-webapp}"
FUNCTION_NAME="${FUNCTION_NAME:-ingest}"
WEBAPP_SA="webapp-sa@${PROJECT_ID}.iam.gserviceaccount.com"
INGEST_SA="ingest-sa@${PROJECT_ID}.iam.gserviceaccount.com"
