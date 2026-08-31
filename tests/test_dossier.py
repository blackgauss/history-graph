"""The lineage dossier: bridge-ranked ancestors, momentum-ranked descendants."""

from pathlib import Path

from history_graph.dossier import lineage_dossier, momentum
from history_graph.mine import FixtureClient


def fixture_client():
    import json
    return FixtureClient(json.loads(Path("tests/fixtures/graph_nlp.json").read_text())["works"])


def test_momentum_uses_recent_acceleration_only():
    fading = {"counts_by_year": [
        {"year": y, "cited_by_count": c} for y, c in [(2022, 15), (2023, 14), (2024, 5), (2025, 3)]
    ]}
    rising = {"counts_by_year": [
        {"year": y, "cited_by_count": c} for y, c in [(2022, 1), (2023, 2), (2024, 20), (2025, 90)]
    ]}
    assert momentum(fading)["score"] < 1 < momentum(rising)["score"]


def test_dossier_structure_and_story():
    out = lineage_dossier(fixture_client(), "10.1/p3", cache_dir=Path("data/tmp/test-dossier"))
    assert out["hub"]["title"].startswith("Bridging paper")

    hop1 = {u["id"] for u in out["upstream"] if u["hop"] == 1}
    assert hop1 == {"W101", "W102", "W104"}
    bridges = [u for u in out["upstream"] if u["hop"] == 2 and u["bridge"]]
    assert [b["id"] for b in bridges] == ["W3"] and bridges[0]["support"] == 2

    down = [d["id"] for d in out["downstream"] if d["hop"] == 1]
    assert down == ["W202", "W201"]  # acceleration beats raw citations
    assert out["downstream"][0]["momentum"] > 10

    schools = out["fuses"]
    assert schools == [[
        {"id": "W101", "doi": "10.1/p1", "title": "Distributional counting at scale", "year": 2000},
        {"id": "W104", "doi": "10.1/p4", "title": "Embedding efficiency notes", "year": 2005},
    ], [
        {"id": "W102", "doi": "10.1/p2", "title": "Neural language modelling", "year": 2001},
    ]]
    assert "Fuses 2 research schools" in out["story"]
    assert "[bridge]" in out["story"] and "↑" in out["story"]
