import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import BadRequest, Conflict

from conftest import ingest


def event(name, size=100, generation="1700000000", event_type="OBJECT_FINALIZE"):
    """Shape of a GCS notification delivered to a Pub/Sub-triggered function."""
    obj = {"bucket": "test-bucket", "name": name, "generation": generation, "size": str(size)}
    return SimpleNamespace(data={"message": {
        "attributes": {"eventType": event_type},
        "data": base64.b64encode(json.dumps(obj).encode()).decode(),
    }})


@pytest.fixture
def bq(monkeypatch):
    client = MagicMock(project="test-project")
    monkeypatch.setattr(ingest, "bq", lambda: client)
    return client


@pytest.fixture
def gcs(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(ingest, "gcs", lambda: client)
    return client


@pytest.mark.parametrize("name,table,fmt", [
    ("uploads/sales.csv", "sales", "CSV"),
    ("uploads/events.json", "events", "NEWLINE_DELIMITED_JSON"),
    ("uploads/facts.parquet", "facts", "PARQUET"),
])
def test_loads_supported_formats(bq, gcs, name, table, fmt):
    ingest.ingest(event(name))
    uri, table_id = bq.load_table_from_uri.call_args.args
    kwargs = bq.load_table_from_uri.call_args.kwargs
    assert uri == f"gs://test-bucket/{name}"
    assert table_id == f"test-project.analysis_dataset.{table}"
    assert kwargs["job_config"].source_format == fmt
    assert kwargs["job_id"] == f"ingest_{table}_1700000000"
    gcs.bucket().copy_blob.assert_not_called()


def test_csv_skips_header_row_only_for_csv():
    assert ingest.load_job_config(".csv").skip_leading_rows == 1
    assert ingest.load_job_config(".parquet").skip_leading_rows is None


def test_duplicate_delivery_reuses_existing_job(bq, gcs):
    bq.load_table_from_uri.side_effect = Conflict("Already Exists: Job")
    ingest.ingest(event("uploads/sales.csv"))
    bq.get_job.assert_called_once_with("ingest_sales_1700000000")
    bq.get_job.return_value.result.assert_called_once()


@pytest.mark.parametrize("name,size,reason", [
    ("uploads/dump.sql", 100, "Unsupported"),
    ("uploads/bad-name.csv", 100, "Invalid table name"),
    ("uploads/empty.csv", 0, "empty"),
    ("uploads/huge.csv", 10 * 1024 * 1024, "limit"),
])
def test_invalid_files_are_moved_to_rejected(bq, gcs, name, size, reason):
    ingest.ingest(event(name, size=size))
    bq.load_table_from_uri.assert_not_called()
    bucket = gcs.bucket.return_value
    assert bucket.copy_blob.call_args.args[2] == name.replace("uploads/", "rejected/")
    assert reason in bucket.blob.return_value.metadata["rejection-reason"]
    bucket.blob.return_value.delete.assert_called_once()


def test_bigquery_parse_error_rejects_without_retry(bq, gcs):
    bq.load_table_from_uri.return_value.result.side_effect = BadRequest("CSV table has error at row 7")
    ingest.ingest(event("uploads/sales.csv"))  # must not raise, or Pub/Sub retries forever
    meta = gcs.bucket.return_value.blob.return_value.metadata
    assert "row 7" in meta["rejection-reason"]


def test_transient_errors_propagate_for_retry(bq, gcs):
    bq.load_table_from_uri.return_value.result.side_effect = ConnectionError("reset")
    with pytest.raises(ConnectionError):
        ingest.ingest(event("uploads/sales.csv"))


def test_ignores_other_events_and_prefixes(bq, gcs):
    ingest.ingest(event("uploads/sales.csv", event_type="OBJECT_DELETE"))
    ingest.ingest(event("rejected/sales.csv"))
    bq.load_table_from_uri.assert_not_called()
