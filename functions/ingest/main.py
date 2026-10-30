"""Pub/Sub-triggered loader: GCS object notification -> BigQuery table.

Flow: webapp writes gs://BUCKET/uploads/<table>.<ext>
      -> GCS notification (OBJECT_FINALIZE) publishes to the `uploads` topic
      -> this function loads the file into BIGQUERY_DATASET.<table>

Error handling:
  * Permanent errors (bad name, bad format, BigQuery rejects the data): the file
    is moved to rejected/ with the reason in object metadata and the message is
    acked. Retrying would never succeed.
  * Transient errors (network, 5xx): the exception propagates so Pub/Sub
    redelivers (deployed with --retry).
"""
import base64
import json
import logging
import os
import re

import functions_framework
from google.api_core.exceptions import BadRequest, Conflict, NotFound
from google.cloud import bigquery, storage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GCP_PROJECT", "")
BIGQUERY_DATASET = os.environ.get("BIGQUERY_DATASET", "analysis_dataset")
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024
UPLOAD_PREFIX = "uploads/"
REJECTED_PREFIX = "rejected/"

# Re-validated here, not just in the webapp: anything with write access to the
# bucket can drop a file in uploads/, and the table name is used in SQL.
_TABLE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")
SOURCE_FORMATS = {
    ".csv": bigquery.SourceFormat.CSV,
    ".json": bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    ".parquet": bigquery.SourceFormat.PARQUET,
}

_bq = None
_gcs = None


def bq() -> bigquery.Client:
    global _bq
    if _bq is None:
        _bq = bigquery.Client(project=PROJECT_ID or None)
    return _bq


def gcs() -> storage.Client:
    global _gcs
    if _gcs is None:
        _gcs = storage.Client(project=PROJECT_ID or None)
    return _gcs


class Rejected(Exception):
    """The file can never be loaded; don't retry."""


def parse_object_name(name: str) -> tuple[str, str]:
    """uploads/<table>.<ext> -> (table, ext), or raise Rejected."""
    rel = name[len(UPLOAD_PREFIX):]
    if "/" in rel:
        raise Rejected("Nested paths under uploads/ are not supported")
    table, dot, ext = rel.rpartition(".")
    ext = f".{ext.lower()}" if dot else ""
    if ext not in SOURCE_FORMATS:
        raise Rejected(f"Unsupported file type '{ext or 'none'}'")
    if not _TABLE_NAME_RE.match(table):
        raise Rejected(f"Invalid table name '{table}'")
    return table, ext


def load_job_config(ext: str) -> bigquery.LoadJobConfig:
    config = bigquery.LoadJobConfig(
        source_format=SOURCE_FORMATS[ext],
        autodetect=True,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        max_bad_records=0,  # fail the whole file rather than silently drop rows
    )
    if ext == ".csv":
        config.skip_leading_rows = 1
    return config


def reject(bucket_name: str, name: str, reason: str) -> None:
    """Move the object to rejected/ so the webapp can report why."""
    logger.warning("Rejecting gs://%s/%s: %s", bucket_name, name, reason)
    bucket = gcs().bucket(bucket_name)
    src = bucket.blob(name)
    dest_name = REJECTED_PREFIX + name[len(UPLOAD_PREFIX):]
    try:
        bucket.copy_blob(src, bucket, dest_name)
    except NotFound:
        return  # already moved by an earlier delivery of this message
    dest = bucket.blob(dest_name)
    dest.metadata = {"rejection-reason": reason[:500]}
    dest.patch()
    src.delete()


@functions_framework.cloud_event
def ingest(cloud_event):
    message = cloud_event.data["message"]
    attrs = message.get("attributes", {})
    if attrs.get("eventType") != "OBJECT_FINALIZE":
        return

    obj = json.loads(base64.b64decode(message["data"]))
    bucket_name, name = obj["bucket"], obj["name"]
    generation, size = obj["generation"], int(obj.get("size", 0))
    if not name.startswith(UPLOAD_PREFIX):
        return  # notification is scoped to uploads/, but be defensive

    try:
        table, ext = parse_object_name(name)
        if size == 0:
            raise Rejected("File is empty")
        if size > MAX_BYTES:
            raise Rejected(f"File is {size} bytes; limit is {MAX_BYTES}")
    except Rejected as e:
        reject(bucket_name, name, str(e))
        return

    table_id = f"{bq().project}.{BIGQUERY_DATASET}.{table}"
    uri = f"gs://{bucket_name}/{name}"
    # Pub/Sub is at-least-once. A job id derived from the object generation makes
    # redelivery of the same upload a no-op instead of a second load.
    job_id = f"ingest_{table}_{generation}"

    try:
        job = bq().load_table_from_uri(uri, table_id, job_id=job_id, job_config=load_job_config(ext))
    except Conflict:
        logger.info("Job %s already exists (duplicate delivery); waiting on it", job_id)
        job = bq().get_job(job_id)

    try:
        job.result()
    except BadRequest as e:
        reject(bucket_name, name, f"BigQuery rejected the file: {e.message}")
        return

    logger.info("Loaded %s into %s (%s rows)", uri, table_id, job.output_rows)
