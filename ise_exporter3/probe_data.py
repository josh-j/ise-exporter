"""Turn ENDPOINTS_DATA.PROBE_DATA from a byte stream into attributes.

Cisco documents this column as "binary-encoded data streams (compressed and
non-printable characters)" held in a VARCHAR2. Two things follow, and both are
the reason this module exists rather than a line in the transport.

The first is that it must never be decoded as text. Thin-mode python-oracledb
decodes character columns as UTF-8, and the transport relaxes that to keep one
malformed byte from costing a whole result -- but a *relaxed* decode of genuinely
binary data is worse than a failed one: every byte outside UTF-8 becomes U+FFFD,
which is the tofu an operator sees, and the original byte is gone for good. So
this column is fetched as bytes and arrives here undamaged.

The second is that the framing is undocumented. Rather than guess at it, every
candidate framing below is *self-validating*: it is accepted only if it consumes
the buffer exactly, with every token decoding cleanly. A framing that does not
fit is declined, and the bytes are handed back base64-encoded with the format
named as far as its magic number allows. That is the difference between "these
are the attributes" and "this looked plausible": a wrong split would read as
data, and profiling attributes that are quietly wrong are worse than absent.
"""
import base64
import gzip
import re
import struct
import zlib


# Columns whose VARCHAR2 holds bytes rather than text. Named rather than
# detected: a column is binary because Cisco documents it as binary, not because
# one appliance happened to return something undecodable today.
BINARY_COLUMNS = frozenset({"PROBE_DATA"})

# Magic numbers that identify a container before anything is parsed out of it.
# Reported even when the payload underneath cannot be framed, because naming the
# format is most of the work of learning to read it.
_GZIP = b"\x1f\x8b"
_JAVA_SERIALIZED = b"\xac\xed"
_ZLIB_FIRST = 0x78
_ZLIB_SECOND = frozenset({0x01, 0x5E, 0x9C, 0xDA})

# A token has to look like an attribute name or value to count as one. ISE
# profiling attributes are ASCII: dhcp-class-identifier, host-name, OUI.
_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{3,}")
_MAX_TOKEN = 4096


def decode(value, *, ceiling=None):
    """Decode one PROBE_DATA field into a described object.

    Always returns a dict with the same three keys, so a caller never has to
    test the shape before reading it:

    ``encoding``    what the bytes turned out to be
    ``attributes``  the name/value pairs, empty when the framing was declined
    ``raw``         base64 of the original bytes, present only when something
                    was left unparsed and a human may need to look at it

    ``None`` in, ``None`` out: an endpoint with no probe data has no object.
    """
    if value is None:
        return None
    payload = _as_bytes(value)
    if payload is None:
        # A str means the value was decoded as text somewhere upstream, so the
        # non-UTF-8 bytes are already lost. Say that rather than parse the
        # damage: U+FFFD in, nonsense out.
        return {
            "encoding": "text",
            "attributes": {},
            "raw": str(value),
        }
    if not payload:
        return {"encoding": "empty", "attributes": {}}

    container, body = _uncontain(payload)
    ise = _read_ise_tlv(body)
    if ise is not None:
        return _described(ise, container)
    framing, tokens = _frame(body)
    if framing is None:
        return _undecoded(container, payload, body, ceiling)

    encoding = framing if container is None else f"{container}+{framing}"
    attributes = _pair(tokens)
    if attributes is None:
        # The framing validated, so the tokens are real -- but nothing about
        # them says which are names and which are values. Pairing anyway would
        # offset every name by one, which is exactly the silent corruption this
        # module refuses. The tokens are handed over as tokens.
        return {
            "encoding": encoding,
            "attributes": {},
            "tokens": tokens,
            "raw": _b64(payload, ceiling),
        }
    return {"encoding": encoding, "attributes": attributes}


def _pair(tokens):
    """Name/value pairs, or None when the tokens do not say how they pair.

    Two shapes, and the self-describing one wins. If every token carries its
    own ``name=value``, the pairing is stated by the data and no alignment is
    being assumed. Failing that, an even count is read as alternating names and
    values -- the shape a length-prefixed map serialises to. An odd count says
    neither, and saying so beats guessing.
    """
    if not tokens:
        return {}
    if all("=" in token[1:] for token in tokens):
        pairs = [token.split("=", 1) for token in tokens]
        return {name: value for name, value in pairs}
    if len(tokens) % 2 == 0:
        return dict(zip(tokens[0::2], tokens[1::2]))
    return None


def _as_bytes(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    return None


def _uncontain(payload):
    """Strip a compression container, if the magic number says there is one."""
    if payload.startswith(_GZIP):
        try:
            return "gzip", gzip.decompress(payload)
        except (OSError, EOFError, zlib.error):
            return None, payload
    if (len(payload) > 1 and payload[0] == _ZLIB_FIRST
            and payload[1] in _ZLIB_SECOND):
        try:
            return "zlib", zlib.decompress(payload)
        except zlib.error:
            return None, payload
    return None, payload


# --- the framing ISE actually uses ------------------------------------------
#
# Read off a live appliance (ISE 3.3 Patch 11) rather than guessed at:
#
#   <varint pairs>  ( 0x11 <varint length> <utf-8 bytes> )*
#
# Base-128 varints, low group first, exactly as protobuf writes them. Tokens
# alternate name, value; an absent value is a zero-length token rather than an
# omitted one, so the alternation holds all the way through.
#
# The header is the useful part. It states how many pairs ISE serialised, which
# is what makes the truncation below detectable instead of invisible. Cisco's
# view exposes only the first 2000 bytes of the profiling LOB, so on a busy
# endpoint the header declares 137 pairs and the projection carries 53.
_ISE_TAG = 0x11
_MIN_ISE_TOKENS = 4


def _read_varint(body, offset):
    value = shift = 0
    while offset < len(body):
        byte = body[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 35:
            return None, offset
    return None, offset


def _read_ise_tlv(body):
    """Parse ISE's own framing, or None if this is not it.

    Truncation is expected rather than exceptional, so a buffer that ends
    partway through a record is still a successful parse of everything before
    it -- but only because every record is anchored by its tag byte. Random
    data does not produce a run of well-formed 0x11 records, which is what
    makes accepting a partial parse safe here and nowhere else in this module.
    """
    declared, offset = _read_varint(body, 0)
    if declared is None:
        return None
    tokens, truncated = [], False
    while offset < len(body):
        if body[offset] != _ISE_TAG:
            # A byte that is not a tag where a tag must be. Not this framing --
            # or a truncation that landed mid-record, which the caller cannot
            # tell apart and must not guess at.
            truncated = True
            break
        size, after = _read_varint(body, offset + 1)
        if size is None or after + size > len(body):
            truncated = True
            break
        chunk = body[after:after + size]
        try:
            tokens.append(chunk.decode("utf-8"))
        except UnicodeDecodeError:
            return None
        offset = after + size
    if len(tokens) < _MIN_ISE_TOKENS:
        return None
    if len(tokens) % 2:
        # A name whose value did not fit. Half a pair is not an attribute.
        tokens.pop()
        truncated = True
    pairs = len(tokens) // 2
    return {
        "attributes": dict(zip(tokens[0::2], tokens[1::2])),
        "declared": declared,
        "pairs": pairs,
        "truncated": truncated or declared != pairs,
    }


def _described(parsed, container):
    """The ISE-framed result, with the truncation stated in the field itself."""
    encoding = "ise-tlv" if container is None else f"{container}+ise-tlv"
    field = {
        "encoding": encoding,
        "attributes": parsed["attributes"],
        "count": parsed["pairs"],
        "declared": parsed["declared"],
        "truncated": parsed["truncated"],
    }
    if parsed["truncated"]:
        missing = max(0, parsed["declared"] - parsed["pairs"])
        # Said in words on the field, because the difference between "this
        # endpoint has 53 attributes" and "this endpoint has 137 attributes and
        # Data Connect shows 53" is the difference between a wrong answer and a
        # partial one, and only the field knows which this is.
        #
        # The cut is Cisco's and it is deliberate. ENDPOINTS_DATA projects the
        # column as utl_raw.cast_to_varchar2(dbms_lob.substr(EDF_KRYOBUFFER,
        # 2000)): the profiling buffer is a LOB holding the whole attribute
        # set, and the view exposes its first 2000 bytes. Nothing downstream
        # can widen that, so the note points at where the rest still lives
        # rather than at a limit somebody might try to raise here.
        field["note"] = (
            f"ISE serialised {parsed['declared']} attributes; this view exposes "
            f"the first 2000 bytes of the profiling buffer, which held "
            f"{parsed['pairs']}. The other {missing} are in ISE but not "
            "reachable through Data Connect -- read the endpoint over ERS for "
            "the whole set")
    return field


def _frame(body):
    """First framing that accounts for every byte, or (None, []) for none.

    Ordered strictest first. A length prefix has to arithmetically account for
    the whole buffer, which random bytes almost never do, so those readers are
    asked before the separator ones -- and among them the wider prefixes first,
    since a narrow reader is the one that can misread a wide record.
    """
    for name, reader in (
            ("u16-length-prefixed", _read_length_prefixed(2)),
            ("u32-length-prefixed", _read_length_prefixed(4)),
            ("u8-length-prefixed", _read_length_prefixed(1)),
            ("nul-separated", _read_separated(b"\x00"))):
        tokens = reader(body)
        if tokens:
            return name, tokens
    # No fixed framing fitted. ISE may simply be using a byte of its own as the
    # separator -- 0xB6 is a pilcrow in the Latin-1 range and a classic record
    # mark -- so the last resort is to let the data name its own separator, on
    # the same all-or-nothing terms as everything above.
    delimiter, tokens = _read_self_separated(body)
    if tokens:
        return f"0x{delimiter:02x}-separated", tokens
    return None, []


def _decode_token(chunk):
    """One token as text, or None if these bytes are not text at all.

    UTF-8 first, then cp1252: a stream separated by a high byte is very often
    Latin-1-ish text from an appliance that never transcoded, and refusing to
    read it as anything but UTF-8 would decline a format that is really there.
    Either way the result has to be printable, which is what stops arbitrary
    binary from decoding into something that merely has a length.
    """
    for codec in ("utf-8", "cp1252"):
        try:
            text = chunk.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.isprintable():
            return text
    return None


def _read_length_prefixed(width):
    """A reader for <length><bytes> records, the shape Java's writeUTF emits."""
    fmt = {1: ">B", 2: ">H", 4: ">I"}[width]

    def read(body):
        tokens, offset = [], 0
        while offset < len(body):
            if offset + width > len(body):
                return []                       # a truncated header: not this
            (size,) = struct.unpack_from(fmt, body, offset)
            offset += width
            # A zero-length record is how a narrower reader misreads a wider
            # prefix: the high half of a u32 length reads as an empty u16
            # record, and the rest of the stream then lines up convincingly.
            # No attribute name is empty, so this is the tell, and refusing it
            # is what keeps the two framings from claiming each other's data.
            if not size or size > _MAX_TOKEN or offset + size > len(body):
                return []                       # a length that overruns: not this
            chunk = body[offset:offset + size]
            offset += size
            text = _decode_token(chunk)
            if text is None:
                return []                       # framed, but not text: not this
            tokens.append(text)
        # Consuming the buffer exactly is the whole proof. A framing that ends
        # mid-record has not understood the data, whatever it collected first.
        return tokens if offset == len(body) else []

    return read


def _read_separated(delimiter):
    def read(body):
        if delimiter not in body:
            return []
        parts = body.split(delimiter)
        # A delimiter at either end is a marker, not an empty token.
        while parts and not parts[0]:
            parts.pop(0)
        while parts and not parts[-1]:
            parts.pop()
        if len(parts) < 2:
            return []
        tokens = []
        for part in parts:
            # An interior empty means two delimiters in a row, which no
            # attribute list produces and every false positive does.
            if not part or len(part) > _MAX_TOKEN:
                return []
            text = _decode_token(part)
            if text is None:
                return []
            tokens.append(text)
        return tokens

    return read


def _read_self_separated(body):
    """Let the buffer nominate its own separator byte, then hold it to proof.

    Only bytes that cannot be part of a printable token are candidates, tried
    most frequent first, because the separator of a token stream is by
    definition the thing between every pair of tokens.
    """
    candidates = {}
    for byte in body:
        if byte < 0x20 or byte > 0x7E:
            candidates[byte] = candidates.get(byte, 0) + 1
    for byte, _count in sorted(
            candidates.items(), key=lambda item: (-item[1], item[0])):
        tokens = _read_separated(bytes([byte]))(body)
        if tokens:
            return byte, tokens
    return None, []


def _undecoded(container, payload, body, ceiling):
    """No framing fitted: name what can be named and keep the bytes."""
    if body.startswith(_JAVA_SERIALIZED):
        encoding = "java-serialized"
    elif container is not None:
        encoding = f"{container}+unframed"
    else:
        encoding = "unframed"
    # The printable runs are offered as a hint, never as attributes: they are
    # what a human would notice reading a hex dump, with none of the structure
    # that would make them name/value pairs.
    strings = [text for text in (match.group().decode("ascii").strip()
                                 for match in _PRINTABLE_RUN.finditer(body))
               if text]
    return {
        "encoding": encoding,
        "attributes": {},
        "strings": strings,
        # The head and the byte census are what somebody working the framing
        # out actually needs, and asking an operator to base64-decode the raw
        # field to get them is asking them to do it at 3am. A separator shows
        # up here as a high-count non-printable byte; a container shows up in
        # the first few bytes.
        "head": body[:64].hex(),
        "separators": _census(body),
        "raw": _b64(payload, ceiling),
    }


def _census(body, limit=8):
    """The commonest non-printable bytes, which is where a separator hides."""
    counts = {}
    for byte in body:
        if byte < 0x20 or byte > 0x7E:
            counts[byte] = counts.get(byte, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {f"0x{byte:02x}": count for byte, count in ranked[:limit]}


def _b64(payload, ceiling):
    encoded = "base64:" + base64.b64encode(payload).decode("ascii")
    if ceiling is not None and len(encoded) > ceiling:
        # Truncation is stated in the value, because a silently shortened
        # base64 string decodes to plausible-looking rubbish.
        keep = max(0, ceiling - 64)
        return (encoded[:keep]
                + f"...[truncated at {ceiling} of {len(encoded)} bytes]")
    return encoded


def looks_encoded(text):
    """Whether a *decoded* string still carries the marks of lost bytes.

    Only used to explain a field that came back through the text path: U+FFFD
    is what the transport's lenient decode leaves behind, so its presence in
    probe data means the bytes were destroyed before this module saw them.
    """
    return isinstance(text, str) and "�" in text


__all__ = ["BINARY_COLUMNS", "decode", "looks_encoded"]
