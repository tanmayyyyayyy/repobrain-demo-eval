"""RepoBrain - vendor-neutral safety, cleanup and audit layer for AI-assisted repos."""
__version__ = "0.2.0"

import sys as _sys

if _sys.version_info < (3, 10):
    raise RuntimeError("RepoBrain needs Python 3.10+")
