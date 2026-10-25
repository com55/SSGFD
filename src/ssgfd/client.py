"""High-level Stella Sora Global/EN game-file client."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import warnings
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from filelock import FileLock

from .manifest import decode_manifest, parse_mani
from .models import (
    CatalogStatus,
    DownloadResult,
    FileInfo,
    Resource,
    ResourceUnavailableError,
    TooManyFilesError,
)
from .patch import apply_bspatch
from .recipes import build_recipes, _safe_name

logger = logging.getLogger(__name__)
BASE_URL = "https://nova-static.stellasora.global"
TARGETS = {"windows": "win", "android": "and"}
_MANI_PATCH = re.compile(r"p_(\d+)_m\.ss_(win|and)\.mani\Z")


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _target(platform: str) -> str:
    if platform not in TARGETS:
        raise ValueError(
            f"Unsupported platform: {platform!r}; choose windows or android"
        )
    return platform


def _matches(name: str, pattern: str, method: str) -> bool:
    if method == "auto":
        method = (
            "glob"
            if any(c in pattern for c in "*?[")
            else "regex"
            if any(c in pattern for c in "^$\\+|(")
            else "contains"
        )
    if method == "contains":
        return pattern.lower() in name.lower()
    if method == "starts_with":
        return name.lower().startswith(pattern.lower())
    if method == "ends_with":
        return name.lower().endswith(pattern.lower())
    if method == "glob":
        from fnmatch import fnmatchcase

        return fnmatchcase(name.lower(), pattern.lower())
    if method == "regex":
        return re.search(pattern, name, re.IGNORECASE) is not None
    raise ValueError(f"Invalid filter method: {method}")


class StellaSoraGameFilesDownloader:
    """Query and download complete EN AssetBundles without a game installation."""

    def __init__(self, data_dir: Path | None = None, *, refresh_seconds: int = 3600):
        root = (
            data_dir
            or os.environ.get("SSGFD_DATA_DIR")
            or Path.home() / ".local/share/SSGFD"
        )
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "catalog.db"
        self.refresh_seconds = refresh_seconds
        self._last_failed: set[str] = set()
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS catalogs (platform TEXT PRIMARY KEY, version TEXT NOT NULL, fetched_at REAL NOT NULL, files TEXT NOT NULL)"
            )

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30)

    def _get(self, url: str) -> bytes:
        with urlopen(url, timeout=30) as response:
            return response.read()

    def _resource_url(self, platform: str, resource: Resource) -> str:
        _target(platform)
        if not _safe_name(resource.name) or not _safe_name(
            resource.additional_path or "x"
        ):
            raise ValueError("Unsafe catalog path")
        pieces = [BASE_URL, "res", TARGETS[platform], str(resource.version)]
        if resource.additional_path:
            pieces.extend(resource.additional_path.replace("\\", "/").split("/"))
        pieces.extend(resource.name.replace("\\", "/").split("/"))
        return "/".join(
            quote(piece, safe=":") if i > 0 else piece for i, piece in enumerate(pieces)
        )

    def _resource_bytes(self, platform: str, resource: Resource) -> bytes:
        path = self.data_dir / "raw" / platform / str(resource.version)
        if resource.additional_path:
            path = path / resource.additional_path
        path = path / resource.name
        root = (self.data_dir / "raw" / platform).resolve()
        if not path.resolve().is_relative_to(root):
            raise ValueError("Resource path escapes cache")
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path) + ".lock"):
            if path.is_file():
                cached = path.read_bytes()
                if _md5(cached) == resource.hash.lower():
                    return cached
            data = self._get(self._resource_url(platform, resource))
            if _md5(data) != resource.hash.lower():
                raise ValueError(f"Resource MD5 mismatch: {resource.name}")
            self._atomic_write(path, data)
            return data

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".ssgfd-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _fetch_catalog(self, platform: str) -> tuple[str, list[FileInfo]]:
        short = TARGETS[platform]
        raw = self._get(f"{BASE_URL}/meta/{short}.html")
        resources = decode_manifest(raw)
        rows = {item.name: item for item in resources}
        mani_name = f"ss_{short}.mani"
        root = rows.get(mani_name)
        if root is None:
            raise ValueError(f"Manifest omits {mani_name}")
        hashes = parse_mani(self._resource_bytes(platform, root))
        if not hashes:
            raise ValueError(f"{mani_name} contains no resource hashes")
        patches = []
        for item in resources:
            match = _MANI_PATCH.fullmatch(item.name)
            if match and match[2] == short:
                patches.append((int(match[1]), item))
        current_version = root.version
        for _tier, item in sorted(patches, key=lambda pair: (pair[0], pair[1].version)):
            if item.version < current_version:
                continue
            hashes = parse_mani(self._resource_bytes(platform, item))
            if not hashes:
                raise ValueError(f"{item.name} contains no resource hashes")
            current_version = item.version
        version = hashlib.sha256(raw).hexdigest()[:16]
        files = build_recipes(resources, hashes, platform, version)
        if not files:
            raise ValueError("Manifest has no complete AssetBundle recipes")
        return version, files

    @staticmethod
    def _encode_files(files: list[FileInfo]) -> str:
        return json.dumps([asdict(item) for item in files], separators=(",", ":"))

    @staticmethod
    def _decode_files(raw: str) -> list[FileInfo]:
        files = []
        for item in json.loads(raw):
            recipe = tuple(Resource(**part) for part in item["recipe"])
            files.append(
                FileInfo(
                    item["name"],
                    item["platform"],
                    item["catalog_version"],
                    item["expected_hash"],
                    recipe,
                )
            )
        return files

    def _stored(self, platform: str) -> tuple[CatalogStatus, list[FileInfo]] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT version, fetched_at, files FROM catalogs WHERE platform=?",
                (platform,),
            ).fetchone()
        if row is None:
            return None
        return CatalogStatus(
            platform, row[0], row[1], platform in self._last_failed
        ), self._decode_files(row[2])

    def catalog_status(self, platform: str) -> CatalogStatus | None:
        """Report version, age anchor and refresh-failure state for one platform."""
        _target(platform)
        stored = self._stored(platform)
        return stored[0] if stored else None

    def update(self, platform: str, force: bool = False) -> CatalogStatus:
        """Refresh a catalog; on failure retain a cached latest-known catalog."""
        _target(platform)
        stored = self._stored(platform)
        if (
            stored
            and not force
            and time.time() - stored[0].fetched_at < self.refresh_seconds
        ):
            return stored[0]
        try:
            version, files = self._fetch_catalog(platform)
        except Exception as exc:
            self._last_failed.add(platform)
            if stored:
                warnings.warn(
                    f"Catalog refresh failed for {platform}; using latest-known cached catalog: {exc}",
                    stacklevel=2,
                )
                return self._stored(platform)[0]
            raise ResourceUnavailableError(
                f"Cannot fetch {platform} catalog: {exc}"
            ) from exc
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO catalogs VALUES (?, ?, ?, ?)",
                (platform, version, time.time(), self._encode_files(files)),
            )
        self._last_failed.discard(platform)
        return self._stored(platform)[0]

    def _catalog(self, platform: str) -> list[FileInfo]:
        self.update(platform)
        return self._stored(platform)[1]

    def query(
        self, pattern: str, platform: str, filter_method: str = "auto"
    ) -> list[FileInfo]:
        """Search current or latest-known cached metadata without bundle downloads."""
        _target(platform)
        if not isinstance(pattern, str):
            raise TypeError("pattern must be a string")
        return [
            item
            for item in self._catalog(platform)
            if _matches(item.name, pattern, filter_method)
        ]

    def _materialize(self, file: FileInfo, cache_dir: Path) -> Path:
        _target(file.platform)
        if not _safe_name(file.name) or not re.fullmatch(
            r"[0-9a-fA-F]{32}", file.expected_hash
        ):
            raise ValueError("Invalid FileInfo path or hash")
        root = (Path(cache_dir) / file.platform).resolve()
        path = root / file.name
        if not path.resolve().is_relative_to(root):
            raise ValueError("FileInfo path escapes cache")
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path) + ".lock"):
            if path.is_file() and _md5(path.read_bytes()) == file.expected_hash:
                return path
            if not file.recipe:
                raise ValueError("FileInfo has no resource recipe")
            data = self._resource_bytes(file.platform, file.recipe[0])
            for step in file.recipe[1:]:
                if not re.fullmatch(r"p_\d+_u\..+\.unity3d", step.name):
                    raise ValueError(f"Invalid delta step: {step.name}")
                data = apply_bspatch(data, self._resource_bytes(file.platform, step))
            if _md5(data) != file.expected_hash:
                raise ValueError(f"Final bundle MD5 mismatch: {file.name}")
            self._atomic_write(path, data)
            return path

    def _selected(
        self,
        pattern_or_selected,
        platform: str | None,
        filter_method: str,
        max_files: int | None,
    ) -> list[FileInfo]:
        if (
            isinstance(pattern_or_selected, list)
            and pattern_or_selected
            and all(isinstance(x, FileInfo) for x in pattern_or_selected)
        ):
            target = platform or pattern_or_selected[0].platform
            _target(target)
            if any(x.platform != target for x in pattern_or_selected):
                raise ValueError("Selected files must share one platform")
            current = {item.name: item for item in self._catalog(target)}
            chosen = []
            for old in pattern_or_selected:
                item = current.get(old.name)
                if item is None:
                    warnings.warn(
                        f"Selected file is absent from current catalog: {old.name}",
                        stacklevel=2,
                    )
                    continue
                if item not in chosen:
                    chosen.append(item)
            return chosen
        if platform is None:
            raise ValueError("platform is required for pattern selection")
        _target(platform)
        if isinstance(pattern_or_selected, str):
            patterns = [pattern_or_selected]
        elif (
            isinstance(pattern_or_selected, list)
            and pattern_or_selected
            and all(isinstance(x, str) for x in pattern_or_selected)
        ):
            patterns = pattern_or_selected
        else:
            raise ValueError(
                "Expected a pattern, nonempty pattern list, or nonempty FileInfo list"
            )
        chosen = [
            item
            for item in self._catalog(platform)
            if any(_matches(item.name, p, filter_method) for p in patterns)
        ]
        if max_files is not None and len(chosen) > max_files:
            raise TooManyFilesError(f"{len(chosen)} files match (limit {max_files})")
        return chosen

    def get_latest_files(
        self,
        pattern_or_selected,
        platform: str | None = None,
        *,
        cache_dir: Path | None = None,
        filter_method: str = "auto",
        max_files: int | None = 50,
    ) -> list[Path]:
        """Return verified complete bundle paths from the newest readable catalog."""
        selected = self._selected(
            pattern_or_selected, platform, filter_method, max_files
        )
        if not selected:
            return []
        root = Path(cache_dir or self.data_dir / "download_cache")
        paths = []
        for item in selected:
            try:
                paths.append(self._materialize(item, root))
            except Exception as exc:
                warnings.warn(f"Cannot retrieve {item.name}: {exc}", stacklevel=2)
        if not paths:
            raise ResourceUnavailableError("No matched bundle could be retrieved")
        if len(paths) != len(selected):
            warnings.warn(
                f"Retrieved {len(paths)} of {len(selected)} matched bundles",
                stacklevel=2,
            )
        return paths

    def _deliver(
        self, selected: list[FileInfo], output_dir: Path, with_path: bool
    ) -> DownloadResult:
        output_dir = Path(output_dir)
        paths = []
        for item in selected:
            try:
                source = self._materialize(item, self.data_dir / "download_cache")
                relative = item.name if with_path else Path(item.name).name
                destination = output_dir / relative
                if not destination.resolve().is_relative_to(output_dir.resolve()):
                    raise ValueError("Output path escapes destination")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with FileLock(str(destination) + ".lock"):
                    if (
                        not destination.is_file()
                        or _md5(destination.read_bytes()) != item.expected_hash
                    ):
                        fd, tmp_name = tempfile.mkstemp(
                            dir=destination.parent, prefix=".ssgfd-"
                        )
                        os.close(fd)
                        try:
                            shutil.copyfile(source, tmp_name)
                            os.replace(tmp_name, destination)
                        finally:
                            if os.path.exists(tmp_name):
                                os.unlink(tmp_name)
                paths.append(destination)
            except Exception as exc:
                warnings.warn(f"Cannot deliver {item.name}: {exc}", stacklevel=2)
        if selected and not paths:
            raise ResourceUnavailableError("No matched bundle could be delivered")
        if len(paths) != len(selected):
            warnings.warn(
                f"Delivered {len(paths)} of {len(selected)} matched bundles",
                stacklevel=2,
            )
        return DownloadResult(
            paths, output_dir, sum(path.stat().st_size for path in paths)
        )

    def download(
        self,
        pattern: str,
        platform: str,
        *,
        output_dir: Path = Path("download"),
        with_path: bool = False,
        filter_method: str = "auto",
        max_files: int | None = 50,
    ) -> DownloadResult:
        selected = self._selected(pattern, platform, filter_method, max_files)
        return self._deliver(selected, output_dir, with_path)

    def download_files(
        self,
        selected: list[FileInfo],
        *,
        output_dir: Path = Path("download"),
        with_path: bool = False,
    ) -> DownloadResult:
        if not selected or not all(isinstance(item, FileInfo) for item in selected):
            raise ValueError("Expected a nonempty FileInfo list")
        if len({item.platform for item in selected}) != 1:
            raise ValueError("Selected files must share one platform")
        return self._deliver(selected, output_dir, with_path)

    def clean(self, platform: str) -> None:
        _target(platform)
        with self._db() as db:
            db.execute("DELETE FROM catalogs WHERE platform=?", (platform,))
        for name in ("raw", "download_cache"):
            shutil.rmtree(self.data_dir / name / platform, ignore_errors=True)
