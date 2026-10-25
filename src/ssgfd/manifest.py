"""Stella Sora encrypted protobuf and text manifest readers."""

from __future__ import annotations

import gzip
import re

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .models import Resource

EN_KEY = b"ma5Dn2FhC*Xhxy%c"
_MD5 = re.compile(r"[0-9a-fA-F]{32}\Z")


def _chunked(raw: bytes) -> bytes | None:
    if b"\r\n" not in raw[:32]:
        return None
    output = bytearray()
    offset = 0
    while offset < len(raw):
        end = raw.find(b"\r\n", offset)
        if end < 0:
            return None
        try:
            length = int(raw[offset:end].split(b";", 1)[0], 16)
        except ValueError:
            return None
        offset = end + 2
        if length == 0:
            return bytes(output)
        if (
            offset + length + 2 > len(raw)
            or raw[offset + length : offset + length + 2] != b"\r\n"
        ):
            return None
        output.extend(raw[offset : offset + length])
        offset += length + 2
    return None


def decrypt_manifest(raw: bytes, key: bytes = EN_KEY) -> bytes:
    """Decode optional transport wrappers, then AES-128-CBC/PKCS7."""
    payload = _chunked(raw) or raw
    if payload.startswith(b"\x1f\x8b"):
        payload = gzip.decompress(payload)
    if len(key) != 16 or len(payload) < 32 or (len(payload) - 16) % 16:
        raise ValueError("Invalid encrypted manifest length or key")
    iv, ciphertext = payload[:16], payload[16:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("Truncated protobuf varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise ValueError("Overlong protobuf varint")


def _fields(data: bytes):
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 2:
            size, pos = _varint(data, pos)
            if size > len(data) - pos:
                raise ValueError("Truncated protobuf field")
            value = data[pos : pos + size]
            pos += size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if size > len(data) - pos:
                raise ValueError("Truncated protobuf field")
            value = data[pos : pos + size]
            pos += size
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire}")
        yield number, wire, value


def decode_manifest(raw: bytes) -> list[Resource]:
    """Decode Pb_ClientDiff/FileDiff from an encrypted server response."""
    plain = decrypt_manifest(raw)
    resources: list[Resource] = []
    for number, wire, entry in _fields(plain):
        if number != 1 or wire != 2:
            continue
        fields = {n: value for n, _w, value in _fields(entry)}
        try:
            name = fields[1].decode("utf-8")
            hash_value = fields[2].decode("ascii")
            version = fields[3]
            additional = fields.get(4, b"").decode("utf-8")
        except (KeyError, UnicodeError, AttributeError) as exc:
            raise ValueError("Malformed Stella Sora resource record") from exc
        if (
            not name
            or not _MD5.fullmatch(hash_value)
            or not isinstance(version, int)
            or version < 0
        ):
            raise ValueError("Invalid Stella Sora resource name, MD5, or version")
        resources.append(Resource(name, hash_value.lower(), version, additional))
    if not resources:
        raise ValueError("Manifest contains no resources")
    return resources


def parse_mani(raw: bytes) -> dict[str, str]:
    """Read resource MD5 values from a Stella Sora `.mani` text file."""
    hashes: dict[str, str] = {}
    for line in raw.decode("utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("$"):
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name, digest = parts[0].strip(), parts[1].strip()
        if name and _MD5.fullmatch(digest):
            hashes[name] = digest.lower()
    return hashes
