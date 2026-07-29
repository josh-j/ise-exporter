"""PROBE_DATA decoding: what it will claim, and what it refuses to claim.

Cisco documents this column as a binary stream in a VARCHAR2 and documents
nothing about its framing. These tests are therefore mostly about the refusal:
a framing is accepted only when it accounts for every byte, so the module either
returns attributes it can prove or returns the bytes and says it could not.
Attributes that are quietly misaligned would read exactly like real ones.
"""
import base64
import gzip
import struct
import zlib

import pytest

from ise_exporter3 import probe_data


def u16(*tokens):
    """The <length><bytes> framing Java's writeUTF emits, which is the guess."""
    return b"".join(struct.pack(">H", len(t)) + t for t in tokens)


def u32(*tokens):
    return b"".join(struct.pack(">I", len(t)) + t for t in tokens)


PAIRS = (b"dhcp-class-identifier", b"MSFT 5.0", b"host-name", b"phone-51")


def test_a_length_prefixed_stream_becomes_the_attributes_it_frames():
    decoded = probe_data.decode(u16(*PAIRS))
    assert decoded["encoding"] == "u16-length-prefixed"
    assert decoded["attributes"] == {
        "dhcp-class-identifier": "MSFT 5.0", "host-name": "phone-51"}
    # Nothing was left over, so nothing needs keeping for a human to inspect.
    assert "raw" not in decoded


def test_the_wider_length_prefix_is_recognised_too():
    decoded = probe_data.decode(u32(*PAIRS))
    assert decoded["encoding"] == "u32-length-prefixed"
    assert decoded["attributes"]["host-name"] == "phone-51"


def test_nul_separated_tokens_are_a_framing_as_well():
    decoded = probe_data.decode(b"\x00".join(PAIRS) + b"\x00")
    assert decoded["encoding"] == "nul-separated"
    assert decoded["attributes"]["dhcp-class-identifier"] == "MSFT 5.0"


@pytest.mark.parametrize("container,compress", [
    ("gzip", gzip.compress), ("zlib", zlib.compress)])
def test_a_compressed_stream_is_opened_before_it_is_framed(container, compress):
    decoded = probe_data.decode(compress(u16(*PAIRS)))
    assert decoded["encoding"] == f"{container}+u16-length-prefixed"
    assert decoded["attributes"]["host-name"] == "phone-51"


def test_a_framing_that_ends_mid_record_is_refused_rather_than_truncated():
    # The first record reads perfectly and the second overruns. Returning the
    # first pair would look like a successful parse of a two-pair field.
    body = u16(b"host-name", b"phone-51") + struct.pack(">H", 500) + b"short"
    decoded = probe_data.decode(body)
    assert decoded["attributes"] == {}
    assert decoded["encoding"] in ("unframed", "java-serialized")
    assert decoded["raw"].startswith("base64:")


def test_an_odd_token_count_is_not_paired_off_by_one():
    # Three tokens cannot be name/value pairs. Pairing them would silently
    # marry every name to the wrong value.
    decoded = probe_data.decode(u16(b"host-name", b"phone-51", b"orphan"))
    assert decoded["attributes"] == {}
    assert decoded["tokens"] == ["host-name", "phone-51", "orphan"]
    assert decoded["raw"].startswith("base64:")


def test_unframed_bytes_are_handed_back_whole_and_named():
    payload = b"\xb6\xff\x01\x02 dhcp-class-identifier \xb6\xfe"
    decoded = probe_data.decode(payload)
    assert decoded["attributes"] == {}
    assert decoded["encoding"] == "unframed"
    # Recoverable: the point of not decoding this column as text.
    assert base64.b64decode(decoded["raw"][len("base64:"):]) == payload
    # The readable runs are a hint for whoever has to work the format out.
    assert "dhcp-class-identifier" in decoded["strings"]


def test_a_java_serialized_stream_is_named_even_though_it_is_not_read():
    decoded = probe_data.decode(b"\xac\xed\x00\x05sr\x00\x11java.util.HashMap")
    assert decoded["encoding"] == "java-serialized"
    assert decoded["attributes"] == {}


def test_bytes_that_arrived_as_text_are_reported_as_already_lost():
    # U+FFFD is what a lenient decode leaves behind. Parsing that would be
    # parsing the damage, so the module says where the value went instead.
    decoded = probe_data.decode("host-name��phone-51")
    assert decoded["encoding"] == "text"
    assert decoded["attributes"] == {}
    assert probe_data.looks_encoded(decoded["raw"])


def test_no_probe_data_is_no_object():
    assert probe_data.decode(None) is None
    assert probe_data.decode(b"")["encoding"] == "empty"


def test_a_memoryview_is_read_like_the_bytes_it_is():
    decoded = probe_data.decode(memoryview(u16(*PAIRS)))
    assert decoded["attributes"]["host-name"] == "phone-51"


def test_the_kept_bytes_respect_the_field_ceiling():
    # An unreadable field must not become the largest thing in the result. The
    # truncation is stated in the value, because a shortened base64 string
    # decodes to plausible rubbish and would be trusted.
    decoded = probe_data.decode(b"\xb6" * 8192, ceiling=1024)
    assert len(decoded["raw"]) <= 1024
    assert "truncated" in decoded["raw"]
