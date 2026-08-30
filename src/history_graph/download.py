"""Download PDFs for every known DOI via Sci-Hub.

Reads DOIs from previous stage outputs (``data/thread/papers.jsonl``,
``data/raw/works.jsonl`` and plain DOI lists like ``data/seed/dois.txt``),
stores each PDF under ``data/pdfs/`` and writes a manifest next to them.
Existing files are skipped, so the stage is resumable.

Run with: ``python -m history_graph.download``
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Protocol

import httpx

from .ingest import load_dois
from .observability import instrumented
from .scihub import MIN_REQUEST_INTERVAL_S, SciHubClient, SciHubError

logger = logging.getLogger(__name__)

DEFAULT_INPUTS = (Path("data/thread/papers.jsonl"), Path("data/raw/works.jsonl"))
DEFAULT_PDF_DIR = Path("data/pdfs")
MANIFEST_FILE = "manifest.json"

_DOI_PREFIXES = (
    "https://doi.org/",
    "https://dx.doi.org/",
    "http://doi.org/",
    "http://dx.doi.org/",
    "doi:",
)


class PdfFetcher(Protocol):
    def get_pdf(self, doi: str) -> bytes: ...


def normalize_doi(value: str) -> str:
    """Strip resolver prefixes and lowercase (DOIs are case-insensitive)."""
    doi = value.strip().lower()
    for prefix in _DOI_PREFIXES:
        if doi.startswith(prefix):
            return doi[len(prefix) :]
    return doi


def doi_to_filename(doi: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "_", normalize_doi(doi)) + ".pdf"


def iter_dois(path: Path) -> list[str]:
    """Collect DOIs from a JSONL works file or a plain DOI list, in order."""
    if path.suffix == ".txt":
        return [normalize_doi(doi) for doi in load_dois(path)]
    dois: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        doi = json.loads(line).get("doi")
        if doi:
            dois.append(normalize_doi(doi))
    return dois


@instrumented("stage.download")
def run_download(
    client: PdfFetcher,
    inputs: list[Path],
    pdf_dir: Path,
) -> dict[str, Any]:
    """Download one PDF per distinct DOI; returns the manifest dict."""
    pdf_dir.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    downloaded: dict[str, str] = {}
    failed: dict[str, str] = {}

    for path in inputs:
        if not path.exists():
            logger.warning("Input %s missing - skipping", path)
            continue
        dois = iter_dois(path)
        logger.info("%s: %d DOIs", path, len(dois))
        for doi in dois:
            if doi in seen:
                continue
            seen.add(doi)
            target = pdf_dir / doi_to_filename(doi)
            if target.exists():
                logger.info("Already downloaded %s -> %s", doi, target)
                downloaded[doi] = str(target)
                continue
            try:
                content = client.get_pdf(doi)
            except (SciHubError, httpx.HTTPError, OSError) as exc:  # keep the batch going
                logger.warning("Failed to download %s: %s", doi, exc)
                failed[doi] = str(exc)
                continue
            target.write_bytes(content)
            logger.info("Downloaded %s -> %s (%d bytes)", doi, target, len(content))
            downloaded[doi] = str(target)

    manifest = {
        "dois_seen": len(seen),
        "pdfs_downloaded": len(downloaded),
        "pdfs_failed": len(failed),
        "files": downloaded,
        "failures": failed,
    }
    (pdf_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="history-graph.download", description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        dest="inputs",
        type=Path,
        help="JSONL works file or DOI list (repeatable; default: thread papers + raw works)",
    )
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument(
        "--mirror",
        action="append",
        dest="mirrors",
        help="Sci-Hub mirror base URL (repeatable, tried in order)",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=None,
        help=f"Seconds between HTTP requests (default {MIN_REQUEST_INTERVAL_S:g}s; "
        "sci-hub tolerates ~3 req/min; env SCIHUB_MIN_INTERVAL)",
    )
    args = parser.parse_args(argv)

    min_interval = MIN_REQUEST_INTERVAL_S
    if raw_interval := os.environ.get("SCIHUB_MIN_INTERVAL"):
        try:
            min_interval = float(raw_interval)
        except ValueError:
            parser.exit(
                2, f"SCIHUB_MIN_INTERVAL must be a number of seconds, got {raw_interval!r}\n"
            )

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    mirrors = args.mirrors
    if not mirrors and os.environ.get("SCIHUB_MIRROR"):
        mirrors = [os.environ["SCIHUB_MIRROR"]]

    client = SciHubClient(
        mirrors=mirrors,
        min_interval_s=args.min_interval if args.min_interval is not None else min_interval,
    )
    try:
        manifest = run_download(
            client,
            args.inputs or list(DEFAULT_INPUTS),
            args.pdf_dir,
        )
    finally:
        client.close()

    print("PDF download complete:")
    for key, value in manifest.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
