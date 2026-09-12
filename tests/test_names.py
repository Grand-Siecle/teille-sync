import pytest
from teille_sync.names import pipeline_name, identifier_of


def test_pipeline_name_adds_reconciled_suffix():
    assert pipeline_name("LIV0001") == "LIV0001_reconciled"


def test_pipeline_name_preserves_various_formats():
    assert pipeline_name("DOC12345") == "DOC12345_reconciled"
    assert pipeline_name("A_B_C") == "A_B_C_reconciled"


def test_identifier_of_removes_reconciled_suffix():
    assert identifier_of("LIV0001_reconciled") == "LIV0001"


def test_identifier_of_tolerant_of_missing_suffix():
    """If the name already lacks the suffix, return it unchanged."""
    assert identifier_of("LIV0001") == "LIV0001"


def test_identifier_of_does_not_strip_mid_string_reconciled():
    """Do not strip _reconciled that merely appears mid-string."""
    assert identifier_of("reconciled_LIV0001") == "reconciled_LIV0001"
    assert identifier_of("my_reconciled_file") == "my_reconciled_file"
