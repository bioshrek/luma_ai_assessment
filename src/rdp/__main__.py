"""`python -m rdp` — the same entry point as the `rdp` console script.

Kept so the acceptance tests and `scripts/demo_crash_resume.sh` can spawn the real CLI without
depending on the console script being on `PATH`.
"""

from rdp.interfaces.cli import app

if __name__ == "__main__":
    app()
