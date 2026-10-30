"""Upload validation: reject bad files in the webapp, before anything reaches GCS.

The ingest Cloud Function re-validates (anything with bucket access can write to
uploads/), so these checks are about fast, clear feedback to the user.
"""
import csv
import io
import json
import os
import re

from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {".csv", ".json", ".parquet", ".xlsx", ".xls", ".txt"}
TXT_DELIMITERS = (",", "\t", "|")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024
HEAD_BYTES = 64 * 1024  # how much of each file validate_content() inspects

_TABLE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")
_SQL_KEYWORDS = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "DROP", "CREATE",
    "ALTER", "EXEC", "UNION", "WHERE", "JOIN",
}

# Magic bytes for binary formats: a renamed .exe won't pass as .parquet.
_PARQUET_MAGIC = b"PAR1"
_XLSX_MAGIC = b"PK\x03\x04"            # xlsx is a zip archive
_XLS_MAGIC = b"\xd0\xcf\x11\xe0"       # legacy OLE2 compound file


class ValidationError(ValueError):
    """Raised when an upload or identifier fails validation."""


def validate_table_name(table_name: str) -> str:
    """Allow only BigQuery-safe identifiers, so names can be interpolated into SQL."""
    if not table_name:
        raise ValidationError("Table name cannot be empty")
    if not _TABLE_NAME_RE.match(table_name):
        raise ValidationError(
            "Invalid table name. Use letters, numbers and underscores, "
            "starting with a letter or underscore (max 128 chars)"
        )
    if table_name.upper() in _SQL_KEYWORDS:
        raise ValidationError("Table name cannot be a SQL keyword")
    return table_name


def split_filename(filename: str) -> tuple[str, str]:
    """Sanitise an uploaded filename and return (table_name, extension)."""
    safe = secure_filename(filename or "")
    if not safe:
        raise ValidationError("No file selected")
    stem, ext = os.path.splitext(safe)
    ext = ext.lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(e.lstrip(".") for e in ALLOWED_EXTENSIONS))
        raise ValidationError(f"Unsupported file type '{ext or 'none'}'. Allowed: {allowed}")
    table_name = stem.replace("-", "_").replace(".", "_").lower()
    return validate_table_name(table_name), ext


def validate_content(head: bytes, ext: str) -> None:
    """Check the first bytes of the file match its extension.

    `head` should be the first HEAD_BYTES of the file. This catches renamed or
    corrupt files early; BigQuery still does full schema validation on load.
    """
    if not head:
        raise ValidationError("File is empty")

    if ext == ".parquet":
        if not head.startswith(_PARQUET_MAGIC):
            raise ValidationError("Not a valid Parquet file (missing PAR1 header)")
    elif ext == ".xlsx":
        if not head.startswith(_XLSX_MAGIC):
            raise ValidationError("Not a valid .xlsx file")
    elif ext == ".xls":
        if not head.startswith(_XLS_MAGIC):
            raise ValidationError("Not a valid .xls file")
    elif ext == ".txt":
        text = _decode(head)
        if len(head) >= HEAD_BYTES:
            text = text.rpartition("\n")[0]  # the sample's last line may be cut off
        _parse_txt_table(text)
    elif ext == ".csv":
        text = _decode(head)
        header = next(csv.reader(io.StringIO(text)), [])
        cols = [c.strip() for c in header]
        if len(cols) < 2 or any(not c for c in cols):
            raise ValidationError("CSV must have a header row with at least 2 named columns")
        if len(set(c.lower() for c in cols)) != len(cols):
            raise ValidationError("CSV header has duplicate column names")
    elif ext == ".json":
        # BigQuery needs newline-delimited JSON: one object per line.
        text = _decode(head).lstrip()
        first_line, newline, _ = text.partition("\n")
        if not newline and len(head) >= HEAD_BYTES:
            # First record is longer than our sample; only check its shape.
            if not first_line.startswith("{"):
                raise ValidationError("Each JSON line must be an object")
            return
        try:
            obj = json.loads(first_line)
        except json.JSONDecodeError:
            raise ValidationError(
                "JSON must be newline-delimited (one object per line), e.g. "
                '{"a": 1}\\n{"a": 2}'
            ) from None
        if not isinstance(obj, dict):
            raise ValidationError("Each JSON line must be an object")


def txt_to_csv(raw: bytes) -> bytes:
    """Convert a delimited text file (e.g. saved from Notepad) to CSV bytes."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("File must be UTF-8 encoded") from None
    out = io.StringIO()
    csv.writer(out).writerows(_parse_txt_table(text))
    return out.getvalue().encode("utf-8")


def _parse_txt_table(text: str) -> list[list[str]]:
    """Parse text as a table delimited by comma, tab or pipe.

    A delimiter qualifies if it gives 2+ columns with the same count on every
    line and at least one data row. Free-form notes fail this. Text can't prove
    it's a table: two short lines with one comma each would still pass.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    best = None
    for delimiter in TXT_DELIMITERS:
        try:
            rows = list(csv.reader(lines, delimiter=delimiter))
        except csv.Error:
            continue
        if len(rows) >= 2 and len(rows[0]) >= 2 and all(len(r) == len(rows[0]) for r in rows):
            if best is None or len(rows[0]) > len(best[0]):
                best = rows
    if best is None:
        raise ValidationError(
            "Text file must be a table: a header row plus data rows, with the same "
            "number of columns on every line, separated by commas, tabs or |"
        )
    header = [c.strip() for c in best[0]]
    if any(not c for c in header):
        raise ValidationError("Text file header has an empty column name")
    if len({c.lower() for c in header}) != len(header):
        raise ValidationError("Text file header has duplicate column names")
    return best


def _decode(head: bytes) -> str:
    try:
        # A multi-byte char may be cut at the end of the sample; ignore that tail.
        return head.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as e:
        if e.start >= len(head) - 3:
            return head[: e.start].decode("utf-8-sig")
        raise ValidationError("File must be UTF-8 encoded") from None
