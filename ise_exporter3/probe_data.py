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
    framing, tokens = _frame(body)
    if framing is None:
        return _undecoded(container, payload, body, ceiling)

    encoding = framing if container is None else f"{container}+{framing}"
    if len(tokens) % 2:
        # An odd token count cannot be name/value pairs. The framing validated,
        # so the tokens are real -- but pairing them would offset every name by
        # one, which is exactly the silent corruption this module refuses.
        return {
            "encoding": encoding,
            "attributes": {},
            "tokens": tokens,
            "raw": _b64(payload, ceiling),
        }
    return {
        "encoding": encoding,
        "attributes": dict(zip(tokens[0::2], tokens[1::2])),
    }


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


def _frame(body):
    """First framing that accounts for every byte, or (None, []) for none."""
    for name, reader in (
            ("u16-length-prefixed", _read_length_prefixed(2)),
            ("u32-length-prefixed", _read_length_prefixed(4)),
            ("nul-separated", _read_nul_separated)):
        tokens = reader(body)
        if tokens:
            return name, tokens
    return None, []


def _read_length_prefixed(width):
    """A reader for <length><bytes> records, the shape Java's writeUTF emits."""
    fmt = ">H" if width == 2 else ">I"

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
            try:
                tokens.append(chunk.decode("utf-8"))
            except UnicodeDecodeError:
                return []                       # framed, but not text: not this
        # Consuming the buffer exactly is the whole proof. A framing that ends
        # mid-record has not understood the data, whatever it collected first.
        return tokens if offset == len(body) else []

    return read


def _read_nul_separated(body):
    if b"\x00" not in body:
        return []
    parts = body.split(b"\x00")
    # A trailing NUL is a terminator, not an empty final token.
    if parts and not parts[-1]:
        parts.pop()
    tokens = []
    for part in parts:
        if not part or len(part) > _MAX_TOKEN:
            return []
        try:
            text = part.decode("utf-8")
        except UnicodeDecodeError:
            return []
        if not text.isprintable():
            return []
        tokens.append(text)
    return tokens


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
        "raw": _b64(payload, ceiling),
    }


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
