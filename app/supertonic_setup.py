"""Install the pinned Supertonic server package for operator setup."""

from __future__ import annotations

import subprocess
import sys


SUPERTONIC_REQUIREMENT = "supertonic[serve]==1.3.1"


def install_supertonic() -> None:
    """Install the pinned sidecar package into the backend virtual environment."""

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", SUPERTONIC_REQUIREMENT],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Supertonic package installation failed.")
