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


# --- the framing ISE actually uses ------------------------------------------
#
# Bytes below are verbatim from ENDPOINTS_DATA.PROBE_DATA on ISE 3.3.0.430
# Patch 11 (laba-ise-001), so these are a drift check on a real serialisation
# and not a restatement of the parser.

# The first 60 bytes of one endpoint's probe data: a varint pair count of 54,
# then 0x11-tagged records holding PolicyVersion=1, assetHwRevision=, and the
# start of EndPointPolicyID.
LAB_PREFIX = bytes.fromhex(
    "36110d506f6c69637956657273696f6e110131110f6173736574487752657669"
    "73696f6e11001110456e64506f696e74506f6c69637949441124")


def ise_tlv(pairs, declared=None):
    """ISE's framing: varint pair count, then 0x11 + varint length + bytes."""
    def varint(value):
        out = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            out.append(byte | (0x80 if value else 0))
            if not value:
                return bytes(out)

    body = varint(len(pairs) if declared is None else declared)
    for name, value in pairs:
        for token in (name.encode(), value.encode()):
            body += bytes([0x11]) + varint(len(token)) + token
    return body


def test_the_framing_ise_actually_uses_is_read():
    decoded = probe_data.decode(ise_tlv([
        ("OUI", "Zabbly"), ("NetworkDeviceName", "campus-corp-wired"),
        ("ip", "10.200.40.144")]))
    assert decoded["encoding"] == "ise-tlv"
    assert decoded["attributes"] == {
        "OUI": "Zabbly",
        "NetworkDeviceName": "campus-corp-wired",
        "ip": "10.200.40.144",
    }
    assert decoded["truncated"] is False
    assert decoded["count"] == decoded["declared"] == 3


def test_an_absent_value_is_an_empty_token_and_keeps_the_alternation():
    # ISE writes a zero-length value rather than omitting the pair. Treating
    # that as a missing token would offset every attribute after it.
    decoded = probe_data.decode(ise_tlv([
        ("PolicyVersion", "1"), ("assetHwRevision", ""),
        ("StaticAssignment", "true")]))
    assert decoded["attributes"] == {
        "PolicyVersion": "1", "assetHwRevision": "", "StaticAssignment": "true"}


def test_a_value_over_127_bytes_needs_the_second_varint_byte():
    # The length is a varint, not a byte. A single-byte reader walks into the
    # middle of the string and every attribute after it is rubbish, which is
    # exactly what the first version of this parser did to real probe data.
    long_value = "Network Access.AuthenticationStatus, " * 8
    decoded = probe_data.decode(ise_tlv([
        ("SelectedAuthorizationProfiles", long_value), ("OUI", "Zabbly")]))
    assert decoded["attributes"]["SelectedAuthorizationProfiles"] == long_value
    assert decoded["attributes"]["OUI"] == "Zabbly"


def test_the_declared_count_is_what_makes_the_truncation_visible():
    # ISE serialises into a column narrower than the data. On a live endpoint
    # the header routinely says 137 pairs and the column carries 53, and an
    # operator reading 53 attributes must not think that is all of them.
    body = ise_tlv([("OUI", "Zabbly"), ("ip", "10.0.0.1")], declared=137)
    decoded = probe_data.decode(body)
    assert decoded["count"] == 2
    assert decoded["declared"] == 137
    assert decoded["truncated"] is True
    assert "135 were cut off in the database" in decoded["note"]


def test_a_pair_cut_in_half_by_the_column_is_dropped_not_half_reported():
    body = ise_tlv([("OUI", "Zabbly"), ("ip", "10.0.0.1"),
                    ("chaddr", "10:66:6a:69:19:42"),
                    ("NetworkDeviceName", "campus-corp-wired")])
    # Chop inside the final value, the way a 2000-byte column does. The name
    # survives the cut and its value does not; reporting the name with an empty
    # value would claim ISE knows this device has no name.
    decoded = probe_data.decode(body[:-4])
    assert "NetworkDeviceName" not in decoded["attributes"]
    assert decoded["attributes"]["chaddr"] == "10:66:6a:69:19:42"
    assert decoded["truncated"] is True


def test_a_handful_of_bytes_is_not_enough_to_claim_the_framing():
    # The tag anchors the parse, but two records is thin evidence and random
    # data can produce that. Real probe data carries fifty pairs and more.
    assert probe_data.decode(ise_tlv([("a", "1")]))["encoding"] != "ise-tlv"


def test_real_appliance_bytes_parse_as_the_appliance_wrote_them():
    decoded = probe_data.decode(LAB_PREFIX)
    assert decoded["encoding"] == "ise-tlv"
    assert decoded["declared"] == 54
    assert decoded["attributes"]["PolicyVersion"] == "1"
    assert decoded["attributes"]["assetHwRevision"] == ""
    # The capture stops mid-record, so the field has to say it is partial.
    assert decoded["truncated"] is True


def test_a_single_byte_length_prefix_is_a_framing_too():
    body = b"".join(bytes([len(t)]) + t for t in PAIRS)
    decoded = probe_data.decode(body)
    assert decoded["encoding"] == "u8-length-prefixed"
    assert decoded["attributes"]["host-name"] == "phone-51"


def test_the_stream_may_nominate_its_own_separator():
    # 0xB6 is a pilcrow in the Latin-1 range and the byte that started all of
    # this by failing to decode as UTF-8. A record mark is exactly what it
    # looks like, so the reader has to be able to find one it was not told
    # about -- on the same all-or-nothing terms as every fixed framing.
    decoded = probe_data.decode(b"\xb6".join(PAIRS))
    assert decoded["encoding"] == "0xb6-separated"
    assert decoded["attributes"] == {
        "dhcp-class-identifier": "MSFT 5.0", "host-name": "phone-51"}


def test_a_leading_or_trailing_separator_is_a_marker_not_a_token():
    decoded = probe_data.decode(b"\x1e" + b"\x1e".join(PAIRS) + b"\x1e")
    assert decoded["encoding"] == "0x1e-separated"
    assert decoded["attributes"]["host-name"] == "phone-51"


def test_tokens_that_carry_their_own_pairing_are_not_realigned():
    # name=value per token says how it pairs. Reading these as alternating
    # names and values would marry 'a=1' to 'b=2' and lose every value.
    decoded = probe_data.decode(
        b"\x1f".join([b"dhcp-class-identifier=MSFT 5.0", b"host-name=phone-51",
                      b"oui=Cisco Systems"]))
    assert decoded["attributes"] == {
        "dhcp-class-identifier": "MSFT 5.0",
        "host-name": "phone-51",
        "oui": "Cisco Systems",
    }


def test_a_self_pairing_stream_survives_an_odd_token_count():
    # Three name=value tokens are three attributes, not an alignment problem.
    decoded = probe_data.decode(b"\x1f".join([b"a=1", b"b=2", b"c=3"]))
    assert decoded["attributes"] == {"a": "1", "b": "2", "c": "3"}


def test_a_latin1_value_is_read_rather_than_declined():
    # An appliance that never transcoded is why this column broke in the first
    # place; refusing to read anything but UTF-8 would decline real data.
    body = "site=Zürich".encode("cp1252") + b"\x1f" + b"host-name=phone-51"
    decoded = probe_data.decode(body)
    assert decoded["attributes"]["site"] == "Zürich"


def test_a_separator_is_only_believed_when_every_token_is_text():
    # Arbitrary binary contains plenty of repeated high bytes. Splitting on the
    # commonest one must not manufacture attributes out of noise.
    decoded = probe_data.decode(bytes(range(256)) * 3)
    assert decoded["attributes"] == {}
    assert decoded["raw"].startswith("base64:")


def test_the_declined_field_carries_what_it_takes_to_identify_the_format():
    payload = b"\xb6\xff\x01\x02 dhcp-class-identifier \xb6\xfe\x01"
    decoded = probe_data.decode(payload)
    assert decoded["attributes"] == {}
    # A hex head names a container; a byte census names a separator. Both
    # without asking anyone to base64-decode the field by hand.
    assert decoded["head"].startswith("b6ff0102")
    assert "0xb6" in decoded["separators"]
    assert decoded["separators"]["0x01"] == 2


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
