"""Mapping between board identifiers and pipeline names.

The board tracks `LIV0001`, the NAS archive is `LIV0001_reconciled.zip`,
and the converter calls the document `LIV0001_reconciled`.
"""


def pipeline_name(identifier):
    """`LIV0001` -> `LIV0001_reconciled`, the name teille-douce uses."""
    return f"{identifier}_reconciled"


def identifier_of(pipeline_name):
    """`LIV0001_reconciled` -> `LIV0001`, the name the board card carries.

    Tolerant of a name that already lacks the suffix (returns it unchanged)
    and does not strip a suffix that merely appears mid-string.
    """
    suffix = "_reconciled"
    if pipeline_name.endswith(suffix):
        return pipeline_name[:-len(suffix)]
    return pipeline_name
