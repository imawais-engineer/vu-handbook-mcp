"""Run the original end-to-end scripts (stdio, HTTP, retrieval eval) under pytest."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script", ["tests/test_mcp.py", "tests/test_http.py",
                                    "tests/eval_queries.py"])
def test_script(script):
    env = {**os.environ, "VU_HTTP_TEST_PORT": "8131"}
    r = subprocess.run([sys.executable, script], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=240)
    assert r.returncode == 0, (r.stdout[-3000:] + r.stderr[-2000:])
