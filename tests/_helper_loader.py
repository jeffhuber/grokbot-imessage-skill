from __future__ import annotations

import importlib.util
import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER_PATH = REPO_ROOT / "bin" / "helper.py"

spec = importlib.util.spec_from_file_location("grokbot_imessage_helper", HELPER_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"could not load helper from {HELPER_PATH}")

helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
