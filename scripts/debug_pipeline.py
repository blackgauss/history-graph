"""Run the offline pipeline under a debugger-friendly entry point.

Usage from the repo root:

    uv run python scripts/debug_pipeline.py                       # plain run
    uv run python -m pdb scripts/debug_pipeline.py                # post-hoc pdb
    HG_BREAK=stage.thread uv run python scripts/debug_pipeline.py # pdb at stage entry
    uv run python scripts/debug_pipeline.py --listen              # debugpy attach

``HG_BREAK`` accepts comma-separated span names (``stage.thread``,
``stage.report``, ``http.openalex``, ...) or ``*``; it calls ``breakpoint()``,
so ``PYTHONBREAKPOINT=ipdb.set_trace`` also works.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("PYTHONHASHSEED", "0")

import _runner  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", type=int, default=None, metavar="PORT",
                        help="wait for a debugpy client to attach on PORT first")
    parser.add_argument("--workdir", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.listen is not None:
        import debugpy

        debugpy.listen(("127.0.0.1", args.listen))
        print(f"waiting for debugpy client on 127.0.0.1:{args.listen}", file=sys.stderr)
        debugpy.wait_for_client()

    result = _runner.run_offline_pipeline(args.workdir or Path(tempfile.mkdtemp(prefix="hg-dbg-")))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
