"""Turns your own recording into a background sound on the Voice page.

    python dashboard/backend/process_sounds.py my_recording.mp3 office    # -> app/sounds/office.wav

Needs ffmpeg. Uploads on the Voice page go through the same app.ambience.process(). Only use audio you may use;
*.wav is git-ignored so a processed file can't be committed by accident. See app/sounds/README.md.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "shared"))

from app import ambience  # noqa: E402

if len(sys.argv) != 3:
    raise SystemExit(__doc__)
source, name = Path(sys.argv[1]), sys.argv[2]
ambience.BUILTIN_DIR.mkdir(exist_ok=True)
print(name, ambience.process(source, ambience.BUILTIN_DIR / f"{name}.wav"))
