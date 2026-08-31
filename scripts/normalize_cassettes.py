"""One-time migration: batch-shaped /works cassette entries -> per-work store.

Reads tests/cassettes/<name>.json, upserts every result record into the
content-addressed store, drops entries fully served by the store, GCs bodies.
Re-runnable; new recordings self-store via the recorder.
"""

from __future__ import annotations

import json
import sys
import urllib.parse as up
from pathlib import Path

from history_graph.testing import CASSETTE_DIR, WorkStore

SERVED_FILTERS = {"openalexid", "doi"}


def main(names: list[str]) -> int:
    for name in names:
        path = CASSETTE_DIR / f"{name}.json"
        store = WorkStore(path.parent / path.stem / "works")
        entries = json.loads(path.read_text(encoding="utf-8"))
        dropped = 0
        for key, entry in list(entries.items()):
            query = dict(up.parse_qsl(key.split("?", 1)[1])) if "?" in key else {}
            try:
                payload = json.loads(Cassette_body(path, entry))
            except ValueError:
                continue
            results = payload.get("results", []) if isinstance(payload, dict) else []
            # every page (even kept citing-cursor pages) feeds the store
            for r in results:
                if isinstance(r, dict):
                    store.upsert(r)
            filt = query.get("filter", "")
            single = filt.count(":") == 1 and "+" not in filt
            if single and filt.split(":", 1)[0].replace("_", "") == "openalexid" \
                    and (payload.get("meta") or {}).get("next") in (None, False):
                wanted = [v.rsplit("/", 1)[-1] for v in filt.split(":", 1)[1].split("|")]
                got = {
                    str(r.get("id", "")).rsplit("/", 1)[-1]
                    for r in results if isinstance(r, dict)
                }
                for wid in wanted:
                    if wid not in got:
                        store.mark_dead(wid)
            if single and filt.split(":", 1)[0].replace("_", "") in SERVED_FILTERS:
                extras = {
                    k for k in query
                    if k not in {"filter", "select", "per-page", "cursor", "mailto"}
                }
                if not extras:
                    entries.pop(key)
                    dropped += 1
        bodies_dir = path.parent / path.stem / "bodies"
        if bodies_dir.exists():
            for p in bodies_dir.glob("*.bin"):
                if not any(v.get("body_file", "").endswith(p.name) for v in entries.values()):
                    p.unlink()
        keep = {p.name for p in (path.parent / path.stem / "works").glob("*.json")}
        path.write_text(
            json.dumps(entries, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(
            f"{name}: kept {len(entries)} generic, dropped {dropped} to store, "
            f"store size {len(keep)}"
        )
    return 0


def Cassette_body(path: Path, entry: dict) -> str:
    if "body_b64" in entry:
        import base64
        return base64.b64decode(entry["body_b64"]).decode("utf-8", "replace")
    return (path.parent / entry["body_file"]).read_text(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["openalex", "openalex-thread"]))
