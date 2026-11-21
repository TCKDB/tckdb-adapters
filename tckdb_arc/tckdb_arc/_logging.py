"""Package logger for ``tckdb_arc``.

Replaces ``arc.common.get_logger`` for the standalone ``tckdb_arc`` package: ARC
returns a single module-level ``logging.getLogger('arc')`` from ``get_logger``;
here the logger is package-local, so ``tckdb_arc`` never has to import ARC just
to log.

The shared upload pipeline in ``tckdb_core`` logs through the instance
(``TCKDBAdapter._log``, which looks up ``tckdb_arc.adapter.logger`` when it is
called) and through the ``log=`` argument of its free functions, so every record
it emits for an ARC run lands under the ``tckdb_arc`` logger with no
process-global state.

The adapter module and the modules split out of it all hold this one object.
Replacing the name ``tckdb_arc.adapter.logger`` redirects only the adapter class
and the upload pipeline; to intercept every record, patch the object's methods
(``mock.patch.object(logger, "warning")``).
"""

import logging

#: Name of the logger every ``tckdb_arc`` record goes to.
LOGGER_NAME = "tckdb_arc"

logger = logging.getLogger(LOGGER_NAME)


def get_logger() -> logging.Logger:
    """Return the package logger (avoids multiple logger entries)."""
    return logger
