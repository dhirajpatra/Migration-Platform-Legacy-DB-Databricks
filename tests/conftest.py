import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests", ROOT):
    sys.path.insert(0, str(p))
