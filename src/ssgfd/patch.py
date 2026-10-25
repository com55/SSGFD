"""Apply BSDIFF40 patches used by Stella Sora resource updates."""

from __future__ import annotations

import bz2


def _signed(data: bytes) -> int:
    if len(data) != 8:
        raise ValueError("Truncated bsdiff integer")
    value = int.from_bytes(data, "little")
    return -(value & ((1 << 63) - 1)) if value & (1 << 63) else value


def apply_bspatch(base: bytes, patch: bytes) -> bytes:
    """Apply a bsdiff 4.x compatible patch, rejecting malformed blocks."""
    if len(patch) < 32 or patch[:8] != b"BSDIFF40":
        raise ValueError("Invalid BSDIFF40 header")
    control_len = _signed(patch[8:16])
    diff_len = _signed(patch[16:24])
    target_len = _signed(patch[24:32])
    if min(control_len, diff_len, target_len) < 0 or 32 + control_len + diff_len > len(
        patch
    ):
        raise ValueError("Invalid BSDIFF40 block sizes")
    try:
        control = bz2.decompress(patch[32 : 32 + control_len])
        diff = bz2.decompress(patch[32 + control_len : 32 + control_len + diff_len])
        extra = bz2.decompress(patch[32 + control_len + diff_len :])
    except OSError as exc:
        raise ValueError("Corrupt BSDIFF40 compressed block") from exc

    output = bytearray()
    old_pos = control_pos = diff_pos = extra_pos = 0
    while len(output) < target_len:
        if control_pos + 24 > len(control):
            raise ValueError("Truncated BSDIFF40 control block")
        add = _signed(control[control_pos : control_pos + 8])
        copy = _signed(control[control_pos + 8 : control_pos + 16])
        seek = _signed(control[control_pos + 16 : control_pos + 24])
        control_pos += 24
        if (
            add < 0
            or copy < 0
            or add + copy == 0
            or len(output) + add + copy > target_len
        ):
            raise ValueError("Invalid BSDIFF40 control tuple")
        if diff_pos + add > len(diff) or extra_pos + copy > len(extra):
            raise ValueError("Truncated BSDIFF40 data block")
        for offset in range(add):
            source_pos = old_pos + offset
            source_byte = base[source_pos] if 0 <= source_pos < len(base) else 0
            output.append((source_byte + diff[diff_pos + offset]) & 255)
        diff_pos += add
        old_pos += add
        output.extend(extra[extra_pos : extra_pos + copy])
        extra_pos += copy
        old_pos += seek
    if len(output) != target_len:
        raise ValueError("BSDIFF40 output size mismatch")
    return bytes(output)
