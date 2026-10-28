"""pytest config: the legacy end-to-end scripts are run as subprocesses by
tests/test_legacy_scripts.py (they have their own runners), so pytest must not
collect their helper coroutines named test_* directly."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

collect_ignore = ["test_http.py", "test_mcp.py", "eval_queries.py"]
