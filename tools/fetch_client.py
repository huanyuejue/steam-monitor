#!/usr/bin/env python3
"""Fetch Steam client binaries for pattern migration.

Polls CHANNEL_MANIFESTS, downloads steamclient64.dll + SteamUI.dll per
new channel build into <out>/<channel>/. Exit 0: new SHAs (publish);
exit 2: all SHAs already published (skip); exit 1: failure (atomic:
nothing is written).

Usage:
    python3 tools/fetch_client.py --out bins/ --published-shas sha1 sha2
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import NamedTuple

CHANNEL_MANIFESTS = {
    "stable": "https://client-update.fastly.steamstatic.com/steam_client_win64",
    "beta": "https://client-update.fastly.steamstatic.com/steam_client_publicbeta_win64",
}
BASE_URL = "https://client-update.fastly.steamstatic.com/"
TARGET_DLLS = ("steamclient64.dll", "SteamUI.dll")

_VERSION_RE = re.compile(r'"version"\s+"(\d+)"')
# Package blocks are indented; the root platform block ("win64"\n{) is not.
_BLOCK_RE = re.compile(r'\r?\n[ \t]+"([a-z0-9_]+)"[ \t]*\r?\n[ \t]*\{([^}]*)\}')
_FILE_RE = re.compile(r'"file"\s+"([^"]+)"')


class Package(NamedTuple):
    name: str
    filename: str


def needs_publish(new_shas: list[str], published_shas: list[str]) -> bool:
    return not set(new_shas) <= set(published_shas)


def _shas_of(entry: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in entry.items() if k != "channel"}


def find_by_shas(
    shas: dict[str, str], versions: dict[str, dict[str, str]]
) -> str | None:
    """按 SHA 组反查已记录的版本号。

    Steam 常让 stable 与 beta 指向同一个 build：两个通道版本号与 DLL 都相同，
    只有先到的通道会进 versions.json。后到的通道按 version 查不到，但 SHA 组
    能对上，说明对应的 TOML 早已发布，不必重复下载与解析。
    """
    wanted = set(shas.values())
    for known_version, entry in versions.items():
        if wanted <= set(_shas_of(entry).values()):
            return known_version
    return None


def resolve_known(
    version: str, versions: dict[str, dict[str, str]], published_shas: list[str]
) -> dict[str, str] | None:
    known = versions.get(version)
    if known is None:
        return None
    shas = _shas_of(known)
    if set(shas.values()) <= set(published_shas):
        return shas
    return None


def extract_targets(data: bytes, names: tuple[str, ...]) -> dict[str, bytes] | None:
    found: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries: dict[str, str] = {}
        for info in z.infolist():
            entries.setdefault(info.filename.rsplit("/", 1)[-1], info.filename)
        for name in names:
            if name not in entries:
                return None
            with z.open(entries[name]) as f:
                found[name] = f.read()
    return found


def select_packages(packages: list[Package]) -> list[str]:
    """bins_win64 first, then other bins_* packages, then the rest."""
    ordered = sorted(
        packages,
        key=lambda p: (0 if p.name == "bins_win64" else 1 if p.name.startswith("bins_") else 2),
    )
    return list(dict.fromkeys(p.filename for p in ordered))


def parse_manifest(text: str) -> tuple[str, list[Package]]:
    """Parse a client manifest (VDF) into (version, packages)."""
    version = _VERSION_RE.search(text)
    if version is None:
        raise ValueError("manifest has no version")
    packages = []
    for block in _BLOCK_RE.finditer(text):
        name, body = block.group(1), block.group(2)
        if name in ("version", "ostype"):
            continue
        found = _FILE_RE.search(body)
        if found is not None:
            packages.append(Package(name, found.group(1)))
    return version.group(1), packages


def fetch_url(url: str) -> bytes:
    """Network adapter (untested seam boundary)."""
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--channels", nargs="*", default=sorted(CHANNEL_MANIFESTS))
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--published-shas", nargs="*", default=[])
    ap.add_argument("--versions-file", default=None)
    args = ap.parse_args()

    unknown = [c for c in args.channels if c not in CHANNEL_MANIFESTS]
    if unknown:
        print(f"unknown channels: {' '.join(unknown)}")
        return 1

    versions: dict[str, dict[str, str]] = {}
    if args.versions_file is not None and Path(args.versions_file).exists():
        versions = json.loads(Path(args.versions_file).read_text())

    fetched: list[tuple[str, str, dict[str, str], dict[str, bytes]]] = []
    # SHA 组已发布但版本号是新的：TOML 不用重发，版本记录仍要补上
    seen_versions: dict[str, dict[str, str]] = {}
    try:
        for channel in args.channels:
            version, packages = parse_manifest(
                fetch_url(CHANNEL_MANIFESTS[channel]).decode("utf-8")
            )
            print(f"client version ({channel}): {version}")

            if resolve_known(version, versions, args.published_shas) is not None:
                print(f"version already published, skipping download ({channel})")
                continue

            dlls: dict[str, bytes] | None = None
            for filename in select_packages(packages):
                print(f"trying {filename} ({channel})")
                found = extract_targets(fetch_url(args.base_url + filename), TARGET_DLLS)
                if found is not None:
                    dlls = found
                    break
            if dlls is None:
                print("no package contained all of " + ", ".join(TARGET_DLLS) + f" ({channel})")
                return 1
            shas = {name: hashlib.sha256(data).hexdigest() for name, data in dlls.items()}

            # 同一组 DLL 可能已被另一个通道发布过（stable 与 beta 常同步指向同一个
            # build）。此时 TOML 早已存在，只需把新版本号补进记录，让按版本号查询
            # 能对上，不必再走一遍解析与提交。
            twin = find_by_shas(shas, versions)
            if twin is not None:
                print(
                    f"identical DLLs already published as {twin}, "
                    f"recording {version} ({channel}) without republishing"
                )
                seen_versions[version] = {"channel": channel, **shas}
                continue

            fetched.append((channel, version, shas, dlls))
    except Exception as e:
        print(f"failed to fetch manifest or package: {e}")
        return 1

    for channel, version, shas, dlls in fetched:
        out = Path(args.out) / channel
        out.mkdir(parents=True, exist_ok=True)
        for name, data in dlls.items():
            (out / name).write_bytes(data)
        (out / "versions.json").write_text(
            json.dumps({"version": version, "channel": channel, **shas}, indent=2) + "\n"
        )
        if args.versions_file is not None:
            versions[version] = {"channel": channel, **shas}
    # 只补齐版本记录的通道：没有新 SHA 要发布，但记录不能丢
    versions.update(seen_versions)

    if args.versions_file is not None and (fetched or seen_versions):
        versions_path = Path(args.versions_file)
        versions_path.parent.mkdir(parents=True, exist_ok=True)
        ordered = dict(sorted(versions.items(), key=lambda kv: int(kv[0])))
        versions_path.write_text(json.dumps(ordered, indent=2) + "\n")

    if not fetched:
        if seen_versions:
            # 只补了版本记录，没有新二进制要解析。用独立退出码与"有新文件"
            # 区分开：调用方据此只提交 versions.json，不走解析与渲染
            print("recorded new versions without publishing: " + " ".join(seen_versions))
            return 3
        print("all versions already published")
        return 2
    new_shas = [s for _, _, shas, _ in fetched for s in shas.values()]
    if needs_publish(new_shas, args.published_shas):
        print("new SHAs: " + " ".join(new_shas))
        return 0
    print("all SHAs already published")
    return 2


if __name__ == "__main__":
    sys.exit(main())
