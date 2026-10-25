"""Protocol and delivery checks using a small synthetic CDN."""

import bz2
import hashlib
import time
import warnings

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ssgfd import ResourceUnavailableError, StellaSoraGameFilesDownloader
from ssgfd.manifest import EN_KEY, decode_manifest, parse_mani
from ssgfd.models import FileInfo, Resource
from ssgfd.patch import apply_bspatch
from ssgfd.recipes import build_recipes


def varint(number):
    result = bytearray()
    while number > 127:
        result.append((number & 127) | 128)
        number >>= 7
    result.append(number)
    return bytes(result)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint(number << 3 | 2) + varint(len(value)) + value


def encrypted_manifest(resources):
    entries = []
    for item in resources:
        record = b"".join(
            (
                field(1, item.name.encode()),
                field(2, item.hash.encode()),
                field(3, item.version),
                field(4, item.additional_path.encode()),
            )
        )
        entries.append(field(1, record))
    padder = padding.PKCS7(128).padder()
    padded = padder.update(b"".join(entries)) + padder.finalize()
    iv = b"0" * 16
    encryptor = Cipher(algorithms.AES(EN_KEY), modes.CBC(iv)).encryptor()
    return iv + encryptor.update(padded) + encryptor.finalize()


def bsdiff_extra(target):
    control = b"".join(x.to_bytes(8, "little") for x in (0, len(target), 0))
    blocks = (bz2.compress(control), bz2.compress(b""), bz2.compress(target))
    header = (
        b"BSDIFF40"
        + len(blocks[0]).to_bytes(8, "little")
        + len(blocks[1]).to_bytes(8, "little")
        + len(target).to_bytes(8, "little")
    )
    return header + b"".join(blocks)


def md5(value):
    return hashlib.md5(value).hexdigest()


def test_manifest_roundtrip_and_truncation():
    resource = Resource("icon-a.unity3d", md5(b"abc"), 12)
    assert decode_manifest(encrypted_manifest([resource])) == [resource]
    with pytest.raises(ValueError):
        decode_manifest(encrypted_manifest([resource])[:-1])


def test_recipes_replacement_delta_and_missing_hash():
    resources = [
        Resource("a.unity3d", md5(b"old"), 1),
        Resource("p_2_u.a.unity3d", md5(b"patch"), 2),
        Resource("p_3_n.a.unity3d", md5(b"new"), 3),
        Resource("p_4_u.a.unity3d", md5(b"later"), 4),
    ]
    files = build_recipes(resources, {"a.unity3d": md5(b"final")}, "windows", "v1")
    assert [step.name for step in files[0].recipe] == [
        "p_3_n.a.unity3d",
        "p_4_u.a.unity3d",
    ]
    assert build_recipes(resources, {}, "windows", "v1") == []
    assert (
        build_recipes(
            [Resource("p_2_u.a.unity3d", md5(b"x"), 2)],
            {"a.unity3d": md5(b"x")},
            "windows",
            "v1",
        )
        == []
    )
    stale = [
        Resource("a.unity3d", md5(b"new"), 10),
        Resource("p_1_u.a.unity3d", md5(b"old"), 9),
    ]
    assert [
        step.name
        for step in build_recipes(stale, {"a.unity3d": md5(b"new")}, "windows", "v1")[
            0
        ].recipe
    ] == ["a.unity3d"]


def test_bspatch_and_bad_patch():
    assert apply_bspatch(b"old", bsdiff_extra(b"new")) == b"new"
    with pytest.raises(ValueError):
        apply_bspatch(b"old", b"bad")


@pytest.mark.parametrize("platform,short", [("windows", "win"), ("android", "and")])
def test_client_query_cache_and_delivery(tmp_path, platform, short):
    base = b"old bundle"
    final = b"new bundle"
    delta = bsdiff_extra(final)
    root_mani = f"a.unity3d|{md5(base)}|{len(base)}|0\nobsolete.unity3d|{md5(base)}|{len(base)}|0\n".encode()
    next_mani = f"a.unity3d|{md5(final)}|{len(final)}|0\n".encode()
    mani_patch = next_mani
    resources = [
        Resource(f"ss_{short}.mani", md5(root_mani), 1),
        Resource(f"p_1_m.ss_{short}.mani", md5(mani_patch), 2),
        Resource("a.unity3d", md5(base), 1),
        Resource("obsolete.unity3d", md5(base), 1),
        Resource("p_1_u.a.unity3d", md5(delta), 2),
    ]
    client = StellaSoraGameFilesDownloader(tmp_path / "data")
    payloads = {
        f"{client._resource_url(platform, item)}": value
        for item, value in zip(resources, (root_mani, mani_patch, base, base, delta))
    }
    payloads[f"https://nova-static.stellasora.global/meta/{short}.html"] = (
        encrypted_manifest(resources)
    )
    calls = []

    def get(url):
        calls.append(url)
        return payloads[url]

    client._get = get
    found = client.query("a.unity3d", platform)
    assert [file.name for file in found] == ["a.unity3d"]
    assert client.query("obsolete.unity3d", platform) == []
    assert len(calls) == 3  # main manifest and two .mani resources; no bundle bytes
    paths = client.get_latest_files(found)
    assert paths[0].read_bytes() == final
    before = len(calls)
    assert client.get_latest_files("a.unity3d", platform)[0] == paths[0]
    assert len(calls) == before
    delivered = client.download_files(found, output_dir=tmp_path / "out")
    assert delivered.files[0].read_bytes() == final
    assert delivered.count == 1


def test_latest_known_fallback_and_zero_delivery(tmp_path):
    client = StellaSoraGameFilesDownloader(tmp_path / "data", refresh_seconds=0)
    base = b"bundle"
    mani = f"a.unity3d|{md5(base)}|6|0\n".encode()
    resources = [
        Resource("ss_win.mani", md5(mani), 1),
        Resource("a.unity3d", md5(base), 1),
    ]
    payloads = {
        client._resource_url("windows", resource): data
        for resource, data in zip(resources, (mani, base))
    }
    payloads["https://nova-static.stellasora.global/meta/win.html"] = (
        encrypted_manifest(resources)
    )
    client._get = payloads.__getitem__
    assert len(client.query("a.unity3d", "windows")) == 1
    client._get = lambda _url: (_ for _ in ()).throw(OSError("offline"))
    with warnings.catch_warnings(record=True) as notices:
        assert len(client.query("a.unity3d", "windows")) == 1
    assert any("latest-known" in str(w.message) for w in notices)
    assert client.catalog_status("windows").last_refresh_failed
    with pytest.raises(ResourceUnavailableError):
        client.get_latest_files("a.unity3d", "windows")


def test_mani_and_path_guard():
    assert parse_mani(b"$GAME_VER:1\na.unity3d|" + md5(b"x").encode() + b"|1|0\n") == {
        "a.unity3d": md5(b"x")
    }
    with pytest.raises(ValueError):
        build_recipes([Resource("../escape.unity3d", md5(b"x"), 1)], {}, "windows", "v")


def test_failed_reconstruction_preserves_existing_cache(tmp_path):
    client = StellaSoraGameFilesDownloader(tmp_path / "data")
    cache_dir = tmp_path / "cache"
    path = cache_dir / "windows" / "a.unity3d"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"previous verified version")
    item = FileInfo(
        "a.unity3d",
        "windows",
        "new",
        md5(b"expected new version"),
        (Resource("a.unity3d", md5(b"bad"), 2),),
    )
    client._get = lambda _url: b"bad"
    with pytest.raises(ValueError, match="Final bundle MD5 mismatch"):
        client._materialize(item, cache_dir)
    assert path.read_bytes() == b"previous verified version"


def test_selected_records_refresh_against_current_catalog(tmp_path):
    client = StellaSoraGameFilesDownloader(tmp_path / "data", refresh_seconds=10_000)
    resource = Resource("a.unity3d", md5(b"a"), 1)
    current = FileInfo("a.unity3d", "windows", "v2", md5(b"a"), (resource,))
    old = FileInfo("a.unity3d", "windows", "v1", md5(b"old"), (resource,))
    stale = FileInfo("gone.unity3d", "windows", "v1", md5(b"gone"), (resource,))
    with client._db() as db:
        db.execute(
            "INSERT INTO catalogs VALUES (?, ?, ?, ?)",
            ("windows", "v2", time.time(), client._encode_files([current])),
        )
    assert client._selected([old], None, "auto", 0) == [current]
    with pytest.warns(UserWarning, match="absent"):
        assert client.get_latest_files([stale]) == []
    assert client._selected(["a*", "*unity3d"], "windows", "glob", 50) == [current]


def test_rejects_unsafe_selected_path(tmp_path):
    client = StellaSoraGameFilesDownloader(tmp_path / "data")
    item = FileInfo(
        "../escape.unity3d",
        "windows",
        "v",
        md5(b"a"),
        (Resource("a.unity3d", md5(b"a"), 1),),
    )
    with pytest.raises(ValueError, match="Invalid FileInfo"):
        client._materialize(item, tmp_path / "cache")
