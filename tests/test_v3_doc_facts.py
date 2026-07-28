"""The curated explorer metadata against Cisco's own Data Connect docs.

``docs/dataconnect-views.json`` is a verbatim transcription of
https://developer.cisco.com/docs/dataconnect/database-views/ -- all 70 views,
every column with its documented type. ``docs/`` is intentionally untracked,
so like the lab-facts suite these tests are inert on a bare clone and gate in
a working checkout.

The lab facts remain the higher authority where the two disagree: the doc
describes what Cisco published, the appliance describes what ISE does.
"""
import json
import pathlib

import pytest

from ise_exporter3.explore import VIEW_CATALOG
from ise_exporter3.transports.dataconnect import SCHEMA_COLUMN_CONTRACTS


DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "dataconnect-views.json"

pytestmark = pytest.mark.skipif(
    not DOC.exists(),
    reason="docs/dataconnect-views.json is not in this checkout")


@pytest.fixture(scope="module")
def official():
    return json.loads(DOC.read_text())["views"]


def _columns(official, view):
    return {column["name"]: column["type"]
            for column in official[view]["columns"]}


def test_every_curated_view_is_documented(official):
    for entry in VIEW_CATALOG.values():
        assert entry.view in official


def test_curated_columns_exist_in_the_documentation(official):
    for entry in VIEW_CATALOG.values():
        columns = _columns(official, entry.view)
        for column in entry.default_columns:
            assert column in columns, f"{entry.view}.{column}"
        if entry.time_column:
            assert entry.time_column in columns, (
                f"{entry.view}.{entry.time_column}")


def test_curated_zoned_columns_are_exactly_the_documented_tstz_set(official):
    # Both directions matter: a curated CAST on a non-date column breaks the
    # statement (TACACS_AUTHORIZATION_LAST_TWO_DAYS documents GENERATED_TIME
    # as VARCHAR2 where its two siblings are zoned), and a missing CAST is
    # DPY-3022 on the thin fetch.
    for entry in VIEW_CATALOG.values():
        columns = _columns(official, entry.view)
        documented = {name for name, dtype in columns.items()
                      if "WITH TIME ZONE" in dtype and "LOCAL" not in dtype}
        assert set(entry.zoned_columns) == documented, entry.view


def test_time_kinds_match_the_documented_types(official):
    kinds = {
        "timestamp": lambda t: t.startswith("TIMESTAMP")
        and "TIME ZONE" not in t,
        "epoch": lambda t: t == "NUMBER",
        "tstz": lambda t: "WITH TIME ZONE" in t and "LOCAL" not in t,
    }
    for entry in VIEW_CATALOG.values():
        if not entry.time_column:
            continue
        dtype = _columns(official, entry.view)[entry.time_column]
        assert kinds[entry.time_kind](dtype), (
            f"{entry.view}.{entry.time_column} is {dtype}, "
            f"curated as {entry.time_kind}")


# The doc's real-time note speaks ISE's internal attribute vocabulary, not
# the view's column names. This is the translation, and it is deliberately
# explicit: joining the two programmatically without it silently drops the
# renamed half of the real-time set.
_NOTE_ATTRIBUTE_TO_COLUMN = {
    "STATIC_ASSIGNEMENT": "STATIC_ASSIGNMENT",      # sic, on the page
    "NMAP_SUBNET_SCAN_ID": "NMAP_SUBNET_SCANID",
    "DEVICE_REG_STATUS": "DEVICE_REGISTRATIONS_STATUS",
    "BYOD_REGISTERED": "BYOD_REG",
    "HOST_NAME": "HOSTNAME",
    "EPID": "ENDPOINT_ID",
}


def test_the_curated_realtime_set_is_the_documented_note_translated(official):
    # Freshness is duty-cycle information: everything outside this set syncs
    # with up to 12 hours' delay, so the set must come from the doc, not from
    # anyone's memory of it. Both directions are pinned -- a curated column
    # the note does not justify, and a note attribute with a column that the
    # curation missed, both fail.
    for entry in VIEW_CATALOG.values():
        attributes = official[entry.view].get("realtime_attributes")
        if not attributes:
            assert entry.realtime_columns == (), entry.view
            continue
        columns = set(_columns(official, entry.view))
        translated = {_NOTE_ATTRIBUTE_TO_COLUMN.get(name, name)
                      for name in attributes}
        assert set(entry.realtime_columns) == translated & columns, entry.view


def test_the_schema_contracts_name_documented_columns(official):
    for view, contract in SCHEMA_COLUMN_CONTRACTS.items():
        columns = _columns(official, view)
        for requirement in ("required", "optional"):
            for column in contract[requirement]:
                assert column in columns, f"{view}.{column}"
