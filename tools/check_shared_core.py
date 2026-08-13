#!/usr/bin/env python3
"""Verify the security-critical iMessage core within and across sibling repos."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from pathlib import Path
from typing import Any


MANIFEST_NAME = "shared-core.json"
SCHEMA_VERSION = 1
IDENTITY_KEYS = (
    "host_display_name",
    "wrapper_name",
    "confirmation_name",
    "helper_version",
    "product_id",
    "launchd_label",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / MANIFEST_NAME
    try:
        mode = manifest_path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ValueError(f"{manifest_path}: manifest must be a regular, non-symlink file")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {manifest_path}: {exc}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"{manifest_path}: expected schema_version {SCHEMA_VERSION}, "
            f"got {manifest.get('schema_version')!r}"
        )
    return manifest


def _normalize_identity(data: bytes, identity: dict[str, Any], path: Path) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: identity-normalized files must be UTF-8") from exc

    substitutions: list[tuple[str, str]] = []
    for key in IDENTITY_KEYS:
        value = identity.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{path}: manifest identity.{key} must be a non-empty string")
        substitutions.append((value, f"__IMESSAGE_{key.upper()}__"))

    # Replace longer values first because product IDs can be substrings of
    # wrapper names and launchd labels.
    for value, replacement in sorted(substitutions, key=lambda item: len(item[0]), reverse=True):
        if value not in text:
            raise ValueError(f"{path}: identity value {value!r} was not found")
        text = text.replace(value, replacement)
    return text.encode("utf-8")


def _manifest_file(root: Path, relative_path: str, logical_name: str) -> Path:
    relative = Path(relative_path)
    if relative == Path(".") or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{logical_name}: path must stay within the repository: {relative}")
    path = root / relative
    current = root
    for component in relative.parts:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise ValueError(f"cannot inspect {current}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise ValueError(f"{logical_name}: path contains a symlink: {current}")
    if not stat.S_ISREG(mode):
        raise ValueError(f"{logical_name}: path is not a regular file: {path}")
    return path


def _fingerprints(root: Path, *, enforce_manifest: bool) -> tuple[str, dict[str, str]]:
    root = root.resolve()
    manifest = _load_manifest(root)
    product = manifest.get("product")
    identity = manifest.get("identity")
    file_specs = manifest.get("files")
    if not isinstance(product, str) or not product:
        raise ValueError(f"{root / MANIFEST_NAME}: product must be a non-empty string")
    if not isinstance(identity, dict):
        raise ValueError(f"{root / MANIFEST_NAME}: identity must be an object")
    if not isinstance(file_specs, list) or not file_specs:
        raise ValueError(f"{root / MANIFEST_NAME}: files must be a non-empty array")

    fingerprints: dict[str, str] = {}
    for spec in file_specs:
        if not isinstance(spec, dict):
            raise ValueError(f"{root / MANIFEST_NAME}: every files entry must be an object")
        logical_name = spec.get("logical_name")
        relative_path = spec.get("path")
        normalization = spec.get("normalization", "none")
        expected = spec.get("sha256")
        if not isinstance(logical_name, str) or not logical_name:
            raise ValueError(f"{root / MANIFEST_NAME}: logical_name must be non-empty")
        if logical_name in fingerprints:
            raise ValueError(f"{root / MANIFEST_NAME}: duplicate logical_name {logical_name!r}")
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError(f"{root / MANIFEST_NAME}: {logical_name} path must be non-empty")
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError(f"{root / MANIFEST_NAME}: {logical_name} sha256 must be 64 hex characters")
        try:
            int(expected, 16)
        except ValueError as exc:
            raise ValueError(
                f"{root / MANIFEST_NAME}: {logical_name} sha256 must be 64 hex characters"
            ) from exc

        path = _manifest_file(root, relative_path, logical_name)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc

        if normalization == "identity":
            data = _normalize_identity(data, identity, path)
        elif normalization != "none":
            raise ValueError(f"{logical_name}: unknown normalization {normalization!r}")

        actual = _sha256(data)
        fingerprints[logical_name] = actual
        if enforce_manifest and actual != expected:
            raise ValueError(
                f"{logical_name}: shared-core hash mismatch\n"
                f"  manifest: {expected}\n"
                f"  actual:   {actual}\n"
                "Review the change in every sibling repo, then update all manifests together."
            )
    return product, fingerprints


def _compare(contracts: list[tuple[Path, str, dict[str, str]]]) -> None:
    baseline_root, baseline_product, baseline = contracts[0]
    for root, product, fingerprints in contracts[1:]:
        if fingerprints != baseline:
            names = sorted(set(baseline) | set(fingerprints))
            differences = [
                name
                for name in names
                if baseline.get(name) != fingerprints.get(name)
            ]
            details = "\n".join(
                f"  {name}: {baseline_product}={baseline.get(name)} {product}={fingerprints.get(name)}"
                for name in differences
            )
            raise ValueError(
                f"shared-core drift between {baseline_root} and {root}:\n{details}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "roots",
        nargs="*",
        type=Path,
        help="repository roots to verify and compare (default: current directory)",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_hashes",
        help="print computed hashes without enforcing manifest hashes",
    )
    args = parser.parse_args(argv)

    roots = args.roots or [Path.cwd()]
    try:
        contracts = []
        for root in roots:
            product, fingerprints = _fingerprints(root, enforce_manifest=not args.print_hashes)
            contracts.append((root.resolve(), product, fingerprints))
        if len(contracts) > 1:
            products = [product for _, product, _ in contracts]
            if len(set(products)) != len(products):
                raise ValueError(f"comparison roots must have distinct products: {products}")
            _compare(contracts)
    except ValueError as exc:
        print(f"shared-core check failed: {exc}", file=sys.stderr)
        return 1

    for root, product, fingerprints in contracts:
        print(f"{product} ({root})")
        for logical_name, digest in fingerprints.items():
            print(f"  {logical_name}: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
