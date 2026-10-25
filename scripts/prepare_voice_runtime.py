"""Prepare pinned offline voice runtime assets used by the Android build."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_IMPLEMENTATION = Path(__file__).resolve().parents[1] / "android" / "tools" / "prepare_voice_runtime.py"
_SPEC = importlib.util.spec_from_file_location("daxqo_prepare_voice_runtime", _IMPLEMENTATION)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to load voice runtime preparation code: {_IMPLEMENTATION}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

prepare = _MODULE.prepare


if __name__ == "__main__":
    raise SystemExit(_MODULE.main())
