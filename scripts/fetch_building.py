#!/usr/bin/env python3
"""
Fetch buildingSMART Duplex Apartment IFC model into data/raw/.
Handles Git LFS pointer detection, fallback sources, integrity verification,
SOURCES.json recording, and optional SVG floor plan generation via IfcConvert.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import urllib.parse

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
PLANS_DIR = RAW_DIR / "plans"
SOURCES_JSON = RAW_DIR / "SOURCES.json"
TARGET_IFC = RAW_DIR / "Duplex_A_20110907.ifc"

SOURCES = [
    {
        "name": "buildingsmart-community/Community-Sample-Test-Files",
        "raw_url": (
            "https://raw.githubusercontent.com/buildingsmart-community/"
            "Community-Sample-Test-Files/main/IFC%202.3.0.1%20(IFC%202x3)/"
            "Duplex%20Apartment/Duplex_A_20110907.ifc"
        ),
        "lfs_url": (
            "https://media.githubusercontent.com/media/buildingsmart-community/"
            "Community-Sample-Test-Files/main/IFC%202.3.0.1%20(IFC%202x3)/"
            "Duplex%20Apartment/Duplex_A_20110907.ifc"
        ),
    },
    {
        "name": "MadsHolten/BOT-Duplex-house",
        "raw_url": (
            "https://raw.githubusercontent.com/MadsHolten/BOT-Duplex-house/"
            "master/Model%20files/IFC/Duplex.ifc"
        ),
        "lfs_url": (
            "https://media.githubusercontent.com/media/MadsHolten/BOT-Duplex-house/"
            "master/Model%20files/IFC/Duplex.ifc"
        ),
    },
]

LICENSE_NOTE = "CC BY 4.0, credit original authors (buildingSMART)"


def compute_sha256(data: bytes) -> str:
    """Compute SHA-256 hash of byte content."""
    return hashlib.sha256(data).hexdigest()


def compute_file_sha256(filepath: Path) -> str:
    """Compute SHA-256 hash of a file on disk."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def is_lfs_pointer(content: bytes) -> bool:
    """Check if file content starts with Git LFS pointer header."""
    return content.startswith(b"version https://git-lfs")


def download_stream(url: str, timeout: int = 30) -> bytes:
    """Download binary content with requests."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def fetch_ifc_content() -> Tuple[bytes, str]:
    """
    Attempt to fetch the IFC model from configured sources in order.
    Detects LFS pointers and retries via media.githubusercontent.com.
    Returns (content_bytes, resolved_source_url).
    """
    for src in SOURCES:
        name = src["name"]
        raw_url = src["raw_url"]
        lfs_url = src["lfs_url"]

        logger.info(f"Trying source: {name} ({raw_url})")
        try:
            content = download_stream(raw_url)
            if is_lfs_pointer(content):
                logger.info(f"LFS pointer detected for {name}, retrying via LFS media URL: {lfs_url}")
                try:
                    content = download_stream(lfs_url)
                    if not is_lfs_pointer(content):
                        return content, lfs_url
                    logger.warning(f"LFS media URL also returned pointer for {name}")
                except Exception as e:
                    logger.warning(f"Failed downloading from LFS media URL {lfs_url}: {e}")
            else:
                return content, raw_url
        except Exception as e:
            logger.warning(f"Failed fetching {raw_url}: {e}")

    raise RuntimeError("Failed to download Duplex Apartment IFC model from all sources.")


def verify_ifc_model(filepath: Path) -> Dict[str, object]:
    """
    Open IFC model with ifcopenshell, confirm schema, and extract entity counts
    and storey elevations.
    """
    import ifcopenshell

    model = ifcopenshell.open(str(filepath))
    schema = model.schema

    storeys = model.by_type("IfcBuildingStorey")
    spaces = model.by_type("IfcSpace")
    walls = model.by_type("IfcWall")
    slabs = model.by_type("IfcSlab")

    storey_info = []
    for s in storeys:
        elev = getattr(s, "Elevation", None)
        storey_info.append({
            "name": getattr(s, "Name", "Unnamed"),
            "elevation": float(elev) if elev is not None else 0.0,
        })

    summary = {
        "schema": schema,
        "storeys_count": len(storeys),
        "spaces_count": len(spaces),
        "walls_count": len(walls),
        "slabs_count": len(slabs),
        "storeys": storey_info,
    }
    return summary


def export_plans_if_available(ifc_path: Path, storey_info: List[Dict[str, object]]) -> None:
    """
    Export SVG floor plans per storey using IfcConvert if available.
    """
    ifc_convert = shutil.which("IfcConvert")
    if not ifc_convert:
        logger.warning("IfcConvert binary not found in PATH. Skipping SVG floor plan generation.")
        return

    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("IfcConvert found. Generating SVG floor plans...")
    for storey in storey_info:
        name = storey["name"]
        safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)
        out_svg = PLANS_DIR / f"{safe_name}.svg"
        cmd = [
            ifc_convert,
            str(ifc_path),
            str(out_svg),
            "--plan",
            "--storey",
            name,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            logger.info(f"Exported plan for '{name}' to {out_svg.name}")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Could not export plan for storey '{name}': {e.stderr or e}")


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # Check idempotency
    if TARGET_IFC.exists() and SOURCES_JSON.exists():
        try:
            with open(SOURCES_JSON, "r") as f:
                saved_meta = json.load(f)
            current_hash = compute_file_sha256(TARGET_IFC)
            if current_hash == saved_meta.get("sha256"):
                logger.info(f"File {TARGET_IFC.name} already exists and matches SHA-256. Skipping download.")
                summary = verify_ifc_model(TARGET_IFC)
                print_summary(summary, current_hash, saved_meta.get("source_url", "unknown"))
                export_plans_if_available(TARGET_IFC, summary["storeys"])
                return
        except Exception as e:
            logger.warning(f"Existing file verification failed: {e}. Re-downloading.")

    content, source_url = fetch_ifc_content()
    sha256_hash = compute_sha256(content)

    TARGET_IFC.write_bytes(content)
    logger.info(f"Saved IFC model to {TARGET_IFC} ({len(content)} bytes)")

    meta = {
        "filename": TARGET_IFC.name,
        "sha256": sha256_hash,
        "source_url": source_url,
        "license": LICENSE_NOTE,
    }
    with open(SOURCES_JSON, "w") as f:
        json.dump(meta, f, indent=2)
    logger.info(f"Saved metadata to {SOURCES_JSON}")

    summary = verify_ifc_model(TARGET_IFC)
    print_summary(summary, sha256_hash, source_url)
    export_plans_if_available(TARGET_IFC, summary["storeys"])


def print_summary(summary: Dict[str, object], sha256: str, source_url: str) -> None:
    print("\n" + "=" * 60)
    print("IFC MODEL VERIFICATION SUMMARY")
    print("=" * 60)
    print(f"Source URL   : {source_url}")
    print(f"SHA-256      : {sha256}")
    print(f"Schema       : {summary['schema']}")
    print(f"Storeys      : {summary['storeys_count']}")
    print(f"Spaces       : {summary['spaces_count']}")
    print(f"Walls        : {summary['walls_count']}")
    print(f"Slabs        : {summary['slabs_count']}")
    print("-" * 60)
    print("Storey Details:")
    for s in summary["storeys"]:
        print(f"  - {s['name']}: Elevation = {s['elevation']} m")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
