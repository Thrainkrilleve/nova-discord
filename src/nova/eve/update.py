"""Keep the SDE current: compare against CCP's latest build and download a new export when needed."""

from __future__ import annotations

import json
import logging
import shutil
import urllib.request
import zipfile
from pathlib import Path

from .sde import SDE, build_index, index_is_current, read_build

log = logging.getLogger(__name__)

BASE_URL = "https://developers.eveonline.com/static-data/tranquility"
LATEST_URL = f"{BASE_URL}/latest.jsonl"


def latest_build() -> dict:
    with urllib.request.urlopen(LATEST_URL, timeout=30) as resp:
        return json.loads(resp.readline())


def local_build(sde_dir: Path) -> int | None:
    try:
        return int(read_build(sde_dir)["buildNumber"])
    except (FileNotFoundError, StopIteration, KeyError, ValueError):
        return None


def download_export(build: int, sde_dir: Path) -> None:
    """Download and unzip an export into sde_dir, replacing what is there only once it succeeded."""
    url = f"{BASE_URL}/eve-online-static-data-{build}-jsonl.zip"
    work = sde_dir.parent
    work.mkdir(parents=True, exist_ok=True)
    zip_path = work / f"sde-{build}.zip"
    staging = work / f"sde-{build}.new"

    log.info("downloading %s", url)
    with urllib.request.urlopen(url, timeout=120) as resp, zip_path.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1 << 20)

    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging)
    zip_path.unlink()

    # The zip may hold the files at its root or inside one folder
    root = staging
    if not (root / "_sde.jsonl").exists():
        subdirs = [p for p in staging.iterdir() if p.is_dir()]
        if len(subdirs) != 1 or not (subdirs[0] / "_sde.jsonl").exists():
            raise RuntimeError(f"unexpected export layout in {url}")
        root = subdirs[0]

    shutil.rmtree(sde_dir, ignore_errors=True)
    root.rename(sde_dir)
    shutil.rmtree(staging, ignore_errors=True)


def update(sde_dir: Path, index_path: Path) -> str:
    """Download a newer export if CCP has one, then make sure the index matches. Returns a summary."""
    latest = int(latest_build()["buildNumber"])
    have = local_build(sde_dir)
    if have is None or have < latest:
        download_export(latest, sde_dir)
        msg = f"SDE updated {have or 'none'} -> {latest}."
    else:
        msg = f"SDE already at the latest build ({have})."
    if not index_is_current(sde_dir, index_path):
        build_index(sde_dir, index_path)
        msg += " Index rebuilt."
    return msg


def open_sde(sde_dir: Path, index_path: Path) -> SDE | None:
    """Open the SDE index, building it first if the export changed. None if there is no export."""
    if local_build(sde_dir) is None:
        if index_path.exists():
            log.warning("no SDE export at %s; using the existing index", sde_dir)
            return SDE(index_path)
        log.warning("no SDE export at %s; EVE item lookups are off (run `nova sde update`)", sde_dir)
        return None
    if not index_is_current(sde_dir, index_path):
        build_index(sde_dir, index_path)
    return SDE(index_path)
