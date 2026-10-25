"""Public records for Stella Sora catalogs and downloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ResourceUnavailableError(RuntimeError):
    """A current match exists, but no complete bundle can be obtained."""


class TooManyFilesError(ValueError):
    """A pattern matches more bundles than the configured limit."""


@dataclass(frozen=True)
class Resource:
    name: str
    hash: str
    version: int
    additional_path: str = ""


@dataclass(frozen=True)
class FileInfo:
    name: str
    platform: str
    catalog_version: str
    expected_hash: str
    recipe: tuple[Resource, ...]


@dataclass(frozen=True)
class CatalogStatus:
    platform: str
    version: str
    fetched_at: float
    last_refresh_failed: bool


@dataclass(frozen=True)
class DownloadResult:
    files: list[Path]
    output_dir: Path
    total_bytes: int

    @property
    def count(self) -> int:
        return len(self.files)
