"""cProfile the offline pipeline and print a pstats table.

Usage: uv run python scripts/profile_pipeline.py [--workdir data/tmp/profile]

Writes cProfile raw data to <workdir>/pipeline.prof plus the HG_PROFILE
span summary to <workdir>/spans.json (set automatically here).
"""

from __future__ import annotations

import argparse
import cProfile
import os
import pstats
import sys
from pathlib import Path

# instrumentation flags must be set before history_graph modules are imported
os.environ.setdefault("HG_PROFILE", "1")
if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.execv(sys.executable, [sys.executable, *sys.argv])  # rerun with hash seed pinned

sys.path.insert(0, str(Path(__file__).parent))
import _runner  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=Path("data/tmp/profile"))
    args = parser.parse_args(argv)

    if os.environ.get("PYTHONHASHSEED") != "0":
        os.environ["PYTHONHASHSEED"] = "0"
        os.execv(sys.executable, [sys.executable, *sys.argv])  # rerun pinned

    args.workdir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HG_PROFILE", "1")
    from history_graph.observability import write_profile

    profiler = cProfile.Profile()
    result = profiler.runcall(_runner.run_offline_pipeline, args.workdir)
    prof_path = args.workdir / "pipeline.prof"
    profiler.dump_stats(prof_path)
    spans = write_profile(args.workdir / "spans.json")

    stats = pstats.Stats(profiler)
    stats.sort_stats(pstats.SortKey.CUMULATIVE).print_stats(15)
    print("\nspan summary (seconds/calls):")
    for name, values in sorted(spans.items()):
        print(f"  {name:24s} {values['seconds']:8.3f}s x{values['calls']}")
    print(f"\nreport: {result['report']}\ncprofile: {prof_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
