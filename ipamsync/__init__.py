"""phpIPAM one-way replication over a git mirror."""

import sys

# Checked at import so it runs before any phpIPAM read. Without it, Python
# 3.6 reads the whole source and only then fails on subprocess.run's
# capture_output. Cron's `python3` is often older than a login shell's.
MIN_PYTHON = (3, 9)

if sys.version_info < MIN_PYTHON:
    # str.format, not an f-string, so old interpreters can parse it.
    sys.exit(
        "phpipam-sync requires Python {}.{} or newer, but this is Python "
        "{}.\n"
        "Invoke it with a new enough interpreter explicitly:\n"
        "    /usr/bin/python3.12 ipam_export.py --config config.yml ...\n"
        "and pin that same path in cron -- do not rely on `python3` or on "
        "the shebang, which resolve through PATH.".format(
            MIN_PYTHON[0], MIN_PYTHON[1], sys.version.split()[0],
        )
    )

# Vendored requests and PyYAML for hosts without pip. Appended, so an
# installed copy wins.
import os as _os  # noqa: E402

_VENDOR = _os.path.join(_os.path.dirname(__file__), "_vendor")
if _VENDOR not in sys.path:
    sys.path.append(_VENDOR)
