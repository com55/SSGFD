"""Build complete AssetBundle recipes from resource records."""

from __future__ import annotations

import re
from collections import defaultdict

from .models import FileInfo, Resource

_PATCH = re.compile(r"p_(\d+)_([num])\.(.+\.unity3d)\Z")


def _safe_name(name: str) -> bool:
    return (
        bool(name)
        and not name.startswith(("/", "\\"))
        and all(
            part not in ("", ".", "..") for part in name.replace("\\", "/").split("/")
        )
        and ":" not in name
    )


def build_recipes(
    resources: list[Resource], hashes: dict[str, str], platform: str, version: str
) -> list[FileInfo]:
    """Select newest replacement and later deltas for each bundle."""
    bases: dict[str, Resource] = {}
    patches: dict[str, list[tuple[int, str, Resource]]] = defaultdict(list)
    for resource in resources:
        if not _safe_name(resource.name) or not _safe_name(
            resource.additional_path or "x"
        ):
            raise ValueError(f"Unsafe catalog path: {resource.name!r}")
        match = _PATCH.fullmatch(resource.name)
        if match:
            patches[match[3]].append((int(match[1]), match[2], resource))
        elif resource.name.endswith(".unity3d"):
            bases[resource.name] = resource

    result: list[FileInfo] = []
    for name in sorted(bases.keys() | patches.keys()):
        steps = sorted(
            patches.get(name, []),
            key=lambda item: (item[0], item[2].version, item[2].name),
        )
        if len({tier for tier, _tag, _resource in steps}) != len(steps):
            continue
        base = bases.get(name)
        recipe = [base] if base else []
        current_version = base.version if base else None
        for _tier, tag, resource in steps:
            if current_version is not None and resource.version < current_version:
                continue
            if tag == "n":
                recipe = [resource]
            elif recipe:
                recipe.append(resource)
            else:
                continue
            current_version = resource.version
        if not recipe:
            continue
        expected = hashes.get(name)
        if not expected or not re.fullmatch(r"[0-9a-fA-F]{32}", expected):
            continue
        result.append(
            FileInfo(name, platform, version, expected.lower(), tuple(recipe))
        )
    return result
