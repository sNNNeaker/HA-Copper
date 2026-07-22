"""Test setup: make the HA-free modules importable without Home Assistant.

`const.py` and `api.py` deliberately have no Home Assistant imports (see
CLAUDE.md), but importing them via the package would execute
`custom_components/copper_labs/__init__.py`, which does need HA. Putting the
component directory itself on sys.path lets tests import them as top-level
modules (`import api`, `import const`) with only `requests` installed.
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "copper_labs")
)
