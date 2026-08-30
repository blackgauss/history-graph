"""Tests for the PDF download runner: DOI collection and manifest output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from history_graph.download import (
    doi_to_filename,
    iter_dois,
    normalize_doi,
    run_download,
)
from history_graph.scihub import SciHubError

PDF = b"%PDF-1.5 fake pdf bytes"


class FakeFetcher:
    def __init__(self, failures: set[str] = frozenset()) -> None:
        self.failures = failures
        self.requested: list[str] = []

    def get_pdf(self, doi: str) -> bytes:
        self.requested.append(doi)
        if doi in self.failures:
            raise SciHubError(f"no pdf for {doi}")
        return PDF


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://doi.org/10.1000/ABC", "10.1000/abc"),
        ("http://dx.doi.org/10.1000/x", "10.1000/x"),
        ("DOI:10.1000/Y", "10.1000/y"),
        (" 10.1000/Space ", "10.1000/space"),
    ],
)
def test_normalize_doi(raw: str, expected: str) -> None:
    assert normalize_doi(raw) == expected


def test_doi_to_filename_is_path_safe() -> None:
    assert doi_to_filename("https://doi.org/10.1038/nature05558") == "10.1038_nature05558.pdf"


def test_iter_dois_from_jsonl_skips_records_without_doi(tmp_path: Path) -> None:
    path = tmp_path / "papers.jsonl"
    write_jsonl(
        path,
        [
            {"id": "a", "doi": "https://doi.org/10.1/a"},
            {"id": "b"},
            {"id": "c", "doi": "https://doi.org/10.1/c"},
        ],
    )
    assert iter_dois(path) == ["10.1/a", "10.1/c"]


def test_iter_dois_from_plain_text_list(tmp_path: Path) -> None:
    path = tmp_path / "dois.txt"
    path.write_text("10.1/a\n\n# comment\nhttps://doi.org/10.1/b\n", encoding="utf-8")
    assert iter_dois(path) == ["10.1/a", "10.1/b"]


def test_run_download_writes_pdfs_and_manifest(tmp_path: Path) -> None:
    papers = tmp_path / "papers.jsonl"
    write_jsonl(
        papers,
        [
            {"id": "a", "doi": "https://doi.org/10.1/a"},
            {"id": "dup", "doi": "https://doi.org/10.1/a"},
            {"id": "b", "doi": "https://doi.org/10.1/b"},
        ],
    )
    pdf_dir = tmp_path / "pdfs"
    fetcher = FakeFetcher()

    manifest = run_download(fetcher, [papers], pdf_dir)

    assert (pdf_dir / "10.1_a.pdf").read_bytes() == PDF
    assert (pdf_dir / "10.1_b.pdf").exists()
    assert fetcher.requested == ["10.1/a", "10.1/b"]
    assert manifest["dois_seen"] == 2
    assert manifest["pdfs_downloaded"] == 2
    assert manifest["pdfs_failed"] == 0


def test_run_download_records_failures_and_continues(tmp_path: Path) -> None:
    papers = tmp_path / "papers.jsonl"
    write_jsonl(
        papers,
        [
            {"id": "a", "doi": "https://doi.org/10.1/a"},
            {"id": "b", "doi": "https://doi.org/10.1/b"},
        ],
    )
    fetcher = FakeFetcher(failures={"10.1/b"})

    manifest = run_download(fetcher, [papers], tmp_path / "pdfs")

    assert manifest["pdfs_downloaded"] == 1
    assert manifest["pdfs_failed"] == 1
    assert "10.1/b" in manifest["failures"]
    assert (tmp_path / "pdfs" / "10.1_a.pdf").exists()


def test_run_download_skips_existing_files(tmp_path: Path) -> None:
    papers = tmp_path / "papers.jsonl"
    write_jsonl(papers, [{"id": "a", "doi": "https://doi.org/10.1/a"}])
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / doi_to_filename("10.1/a")).write_bytes(b"cached")
    fetcher = FakeFetcher()

    manifest = run_download(fetcher, [papers], pdf_dir)

    assert fetcher.requested == []
    assert manifest["pdfs_downloaded"] == 1
    assert (pdf_dir / "10.1_a.pdf").read_bytes() == b"cached"


def test_run_download_ignores_missing_inputs(tmp_path: Path) -> None:
    manifest = run_download(FakeFetcher(), [tmp_path / "nope.jsonl"], tmp_path / "pdfs")
    assert manifest == {
        "dois_seen": 0,
        "pdfs_downloaded": 0,
        "pdfs_failed": 0,
        "files": {},
        "failures": {},
    }
    assert (tmp_path / "pdfs" / "manifest.json").exists()


def test_run_download_propagates_unexpected_errors(tmp_path: Path) -> None:
    papers = tmp_path / "papers.jsonl"
    write_jsonl(papers, [{"id": "a", "doi": "https://doi.org/10.1/a"}])

    class BrokenFetcher:
        def get_pdf(self, doi: str) -> bytes:
            raise TypeError("refactor regression")

    with pytest.raises(TypeError):
        run_download(BrokenFetcher(), [papers], tmp_path / "pdfs")
