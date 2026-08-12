"""grocy_lists reads its config at import time, so the environment has to be
in place before the module is ever imported. conftest is loaded before any
test module, which makes this the only place it can go.

The values are deliberately fake: nothing in these tests touches the network.
The functions under test are the pure ones — formatting, row ownership and
the reconcile plan — which is exactly the part that decides whether the
household's hand-added rows survive a run.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROCY_URL", "https://grocy.invalid")
os.environ.setdefault("GROCY_API_KEY", "test-key-not-real")
os.environ.setdefault("HA_URL", "http://homeassistant.invalid:8123")
os.environ.setdefault("HA_TOKEN", "test-token-not-real")
