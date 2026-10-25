# SSGFD

Stella Sora Game Files Downloader. An independent Python package inspired by [BAGFD](https://github.com/com55/BAGFD), for Global/EN Windows and Android AssetBundles.

The Global/EN Windows and Android manifests were checked live on 2026-10-25. Query returned 114 `char_2d_*` matches on each platform, and one base bundle and one `p_*_u` patched bundle per platform downloaded with final MD5 verification. Game updates may change the format or endpoints.

## Install

```bash
uv sync
```

Run tests with `uv run pytest`. The project does not require the game client or credentials.

## CLI

```bash
uv run ssgfd update windows
uv run ssgfd query windows 'char_2d_*' --filter-method glob
uv run ssgfd download windows 'char_2d_*' -o ./download
uv run ssgfd query android 'icon-*' --filter-method glob --format json
```

Platforms: `windows`, `android`. Region: Global/EN. `query` refreshes catalog metadata when due. The catalog refresh also obtains `.mani` metadata to resolve final bundle hashes; it does not fetch AssetBundle bytes. The default data directory is `~/.local/share/SSGFD`, overridden by `SSGFD_DATA_DIR` or `--data-dir`.

`p_*_m.ss_{platform}.mani` resources are complete plaintext manifest snapshots. The newest applicable snapshot supplies final bundle hashes; `p_*_u.<bundle>` resources are BSDIFF40 deltas applied to bundle bytes.

If the CDN is temporarily unavailable, a previously stored catalog is used with a warning. In that case the results are **latest known from the cached catalog**; the Python API's `catalog_status(platform)` reports its version, fetch time, and failed refresh flag. Every returned bundle passes a final MD5 check. There is no size-only or unchecked mode.

## Python API

```python
from ssgfd import StellaSoraGameFilesDownloader

client = StellaSoraGameFilesDownloader()
matches = client.query("char_2d_", platform="windows")
paths = client.get_latest_files(matches)
result = client.download("char_2d_", platform="windows", output_dir="download")
```

`query()` returns `FileInfo` records. `get_latest_files()` rechecks selected records against the newest readable catalog. `download_files(selected)` uses the supplied metadata snapshot. Pattern lists in `get_latest_files()` form a deduplicated union. Partial retrieval warns and returns completed paths; if current matches exist but none can be delivered, it raises `ResourceUnavailableError`.

The raw resource cache and completed bundle cache are separated by platform. Source resources and final bundles are checked against MD5 values before use. Reconstruction currently holds each selected bundle in memory, so very large bundles may need substantial RAM.

## Format references

The [Stella Sora resource fetcher](https://github.com/MakoStar/ss-resource-fetcher) documents the Global/EN manifest endpoints and encrypted protobuf, `.mani`, and bsdiff format. [BAGFD](https://github.com/com55/BAGFD) informed the query/cache/delivery API design. This project is independent and unaffiliated with Yostar.
