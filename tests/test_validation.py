import pytest

from validation import ValidationError, split_filename, validate_content, validate_table_name


@pytest.mark.parametrize("name", ["sales", "finance_2024", "_staging", "A" * 128])
def test_valid_table_names(name):
    assert validate_table_name(name) == name


@pytest.mark.parametrize("name", [
    "", "1sales", "sales-2024", "a b", "x;DROP TABLE y", "t`", "DROP", "select", "A" * 129,
])
def test_invalid_table_names(name):
    with pytest.raises(ValidationError):
        validate_table_name(name)


@pytest.mark.parametrize("filename,expected", [
    ("Sales Q1.csv", ("sales_q1", ".csv")),
    ("finance-data.JSON", ("finance_data", ".json")),
    ("events.parquet", ("events", ".parquet")),
    ("../../etc/passwd.csv", ("etc_passwd", ".csv")),  # path traversal is stripped
])
def test_split_filename(filename, expected):
    assert split_filename(filename) == expected


@pytest.mark.parametrize("filename", ["dump.sql", "run.exe", "noext", "", "../"])
def test_split_filename_rejects(filename):
    with pytest.raises(ValidationError):
        split_filename(filename)


def test_csv_needs_named_header():
    validate_content(b"department,amount,date\nIT,10,2024-01-01\n", ".csv")
    with pytest.raises(ValidationError, match="header"):
        validate_content(b"justonecolumn\n1\n", ".csv")
    with pytest.raises(ValidationError, match="header"):
        validate_content(b"a,,c\n1,2,3\n", ".csv")
    with pytest.raises(ValidationError, match="duplicate"):
        validate_content(b"a,A\n1,2\n", ".csv")


def test_json_must_be_newline_delimited():
    validate_content(b'{"a": 1}\n{"a": 2}\n', ".json")
    with pytest.raises(ValidationError, match="newline-delimited"):
        validate_content(b'[{"a": 1},\n{"a": 2}]', ".json")
    with pytest.raises(ValidationError, match="object"):
        validate_content(b"[1, 2]\n", ".json")


def test_binary_formats_check_magic_bytes():
    validate_content(b"PAR1\x15\x04", ".parquet")
    with pytest.raises(ValidationError, match="Parquet"):
        validate_content(b"a,b\n1,2\n", ".parquet")  # CSV renamed to .parquet
    validate_content(b"PK\x03\x04rest", ".xlsx")
    with pytest.raises(ValidationError):
        validate_content(b"MZ\x90\x00", ".xlsx")  # Windows executable


def test_empty_and_non_utf8_rejected():
    with pytest.raises(ValidationError, match="empty"):
        validate_content(b"", ".csv")
    with pytest.raises(ValidationError, match="UTF-8"):
        validate_content(b"a,b\n\xff\xfe\x00bad,1\n", ".csv")
