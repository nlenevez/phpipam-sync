"""phpIPAM one-way replication over a git mirror."""

import sys

# Checked at import rather than inside a main(), so it runs before any
# phpIPAM read and cannot be forgotten by a future entry point.
#
# What this replaces is a *late* failure. Under Python 3.6 an export
# reads the entire source, writes the snapshot, and only then dies in the
# git plumbing with:
#
#     TypeError: __init__() got an unexpected keyword argument 'capture_output'
#
# (subprocess.run gained capture_output and text in 3.7) -- a message
# that names neither Python nor a version, arriving after the work looked
# like it had succeeded. Cron is where this bites even on a host that has
# a new enough interpreter installed: the `python3` first on cron's PATH
# is routinely older than the one a login shell resolves.
#
# Below 3.6 this file never gets the chance to run -- the f-strings used
# throughout the tool are a SyntaxError first -- which is its own clear
# enough signal.
MIN_PYTHON = (3, 9)

if sys.version_info < MIN_PYTHON:
    # str.format, not an f-string, so this message survives on the oldest
    # interpreter that can still parse the file.
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
