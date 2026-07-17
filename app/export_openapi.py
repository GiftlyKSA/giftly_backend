"""Export the OpenAPI schema to docs/openapi.json (SPEC SECTION 4.3, 23).

The /docs endpoint is disabled outside development, so CI runs this with
ENVIRONMENT=development to keep a reviewable contract in the repo. A route change
that does not update the committed spec fails the ``docs`` CI job.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import get_settings
from app.main import create_app

_OUTPUT = Path("docs/openapi.json")


def main() -> None:
    """Write the current OpenAPI schema to ``docs/openapi.json``."""
    app = create_app(get_settings())
    schema = app.openapi()
    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {_OUTPUT}")


if __name__ == "__main__":
    main()
