"""The Instagram past-posts feasibility probe: it always refuses a probe, reads no
credentials and does no file, network or database I/O.

usage: python -m scripts.instagram_past_posts_probe [options]

The --workspace, --account, --graph-version and --before options are accepted
and ignored. Every invocation without --help or -h prints a version-1 JSON
refusal and exits 2; --help or -h prints this notice and exits 0.
The source-mediated status and the explicit preview reach a provider
only where the past-posts source is activated.
"""

from __future__ import annotations

import json
import sys


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    print(
        json.dumps(
            {"v": 1, "outcome": "refused", "refusal_code": "feasibility_probe_retired"}
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
