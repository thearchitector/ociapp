from typing import cast

import msgpack

from .errors import PayloadCodecError


def decode_payload(payload: bytes) -> dict[str, object]:
    """Decodes msgpack payload bytes into a Python mapping."""

    try:
        unpacked = msgpack.unpackb(payload, raw=False)
    except (
        ValueError,
        TypeError,
        msgpack.ExtraData,
        msgpack.FormatError,
        msgpack.StackError,
    ) as exc:
        message = "payload is not valid msgpack"
        raise PayloadCodecError(message) from exc

    if not isinstance(unpacked, dict):
        raise PayloadCodecError("payload must decode to a msgpack map")
    if not all(isinstance(key, str) for key in unpacked):
        raise PayloadCodecError("payload keys must be strings")

    return cast("dict[str, object]", unpacked)


def encode_payload(payload: dict[str, object]) -> bytes:
    """Encodes a Python mapping into msgpack payload bytes."""

    try:
        encoded = msgpack.packb(payload, use_bin_type=True)
    except (TypeError, ValueError) as exc:
        message = "payload is not msgpack serializable"
        raise PayloadCodecError(message) from exc

    return cast("bytes", encoded)
