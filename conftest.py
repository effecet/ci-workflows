"""pytest root conftest — add `src/` to sys.path for local dev without editable install.

CI (self-ci.yml) uses `pip install -e ".[dev]"` on a clean runner, so this file is
a local-dev-only accommodation when the sandbox blocks pip from reaching pypi.org.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
