"""Logger factory — stdlib logging only.

:func:`get_logger` returns a configured ``logging.Logger`` and ``default_logger``
is exported. The log file location comes from the config shim (``module_name`` /
``module_logging_path``), resolved by :mod:`astrabox.config.config`.

Rotating **file** handlers are attached when the log directory is writable; if
it is not (e.g. the default points at an absolute path that does not exist in
this environment), the factory falls back to a stderr ``StreamHandler`` and
logs a warning once. Import must not fail merely because the log directory is
unwritable.
"""

from __future__ import annotations

import logging
import os
from logging import Logger
from logging.handlers import RotatingFileHandler
from typing import Optional

from astrabox.config.config import config

MODULE_APP_NAME: str = str(config.get("module_name") or "astrabox")
MODULE_LOGGING_PATH: str = str(config.get("module_logging_path") or "")

_DEFAULT_LOGGER_NAME = "default"
_LEVEL: int = logging.INFO
_MAX_BYTES: int = 500 * 1024 * 1024
_BACKUP_COUNT: int = 5
_ENCODING: str = "utf-8"

_TEXT_FORMATTER = logging.Formatter(
    "%(asctime)s [%(name)s] %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class _JsonFormatter(logging.Formatter):
    """One-line JSON per record — for log aggregators (ELK / Loki / etc.).

    Structured fields: ts, level, logger, msg, plus module/line and any
    exception. Enabled by ``ASTRABOX_LOG_FORMAT=json``; the default stays the
    human text format so a local run is still readable.
    """

    def format(self, record: logging.LogRecord) -> str:
        import json

        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _active_formatter() -> logging.Formatter:
    """The formatter for new handlers: JSON when ``ASTRABOX_LOG_FORMAT=json``."""
    if str(os.getenv("ASTRABOX_LOG_FORMAT", "") or "").strip().lower() == "json":
        return _JsonFormatter()
    return _TEXT_FORMATTER


_FORMATTER = _TEXT_FORMATTER

#: Set once, after the first attempt to attach file handlers, to record whether
#: the configured log directory was usable. When ``False`` the factory uses a
#: stderr stream handler for the application logger.
_FILE_LOGGING_OK: Optional[bool] = None


def _resolve_log_file(name: str) -> Optional[str]:
    """Return the on-disk path for logger ``name``'s file, or ``None``.

    ``None`` when ``module_logging_path`` is unset/empty — there is then no file
    target and the caller uses a stream handler. The directory is created on
    demand; an OS error (missing parent, permission denied, read-only fs) is
    swallowed here and reported by the caller, which falls back to stderr.
    """
    if not MODULE_LOGGING_PATH:
        return None
    log_file = os.path.join(MODULE_LOGGING_PATH, MODULE_APP_NAME, f"{name}.log")
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
    except OSError:
        return None
    return log_file


def _attach_file_handlers(logger: logging.Logger, file_name: str) -> bool:
    """Attach rotating default+error file handlers; return success.

    Returns ``False`` (and attaches nothing) when the log file cannot be opened,
    so the caller can fall back to a stream handler.
    """
    log_file = _resolve_log_file(file_name)
    if log_file is None:
        return False
    try:
        default_handler = RotatingFileHandler(
            filename=log_file,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding=_ENCODING,
        )
    except OSError:
        return False
    default_handler.setLevel(logging.DEBUG)
    default_handler.setFormatter(_active_formatter())
    logger.addHandler(default_handler)

    error_file = _resolve_log_file(f"{file_name}.error") or log_file
    try:
        error_handler = RotatingFileHandler(
            filename=error_file,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding=_ENCODING,
        )
    except OSError:
        return True  # default handler already attached; error handler optional
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(_active_formatter())
    logger.addHandler(error_handler)
    return True


def _attach_stream_handler(logger: logging.Logger) -> None:
    """Attach a single stderr stream handler (the no-file fallback sink)."""
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_active_formatter())
    logger.addHandler(handler)


def _configure_logger(logger: logging.Logger, file_name: str) -> None:
    """Configure ``logger`` with file handlers when possible, else stderr.

    Sets the level, disables propagation at the application namespace boundary,
    and records whether the configured log directory was usable so the warning
    is emitted once.
    """
    global _FILE_LOGGING_OK
    logger.setLevel(_LEVEL)
    logger.propagate = False
    if _attach_file_handlers(logger, file_name):
        if _FILE_LOGGING_OK is None:
            _FILE_LOGGING_OK = True
        return
    _attach_stream_handler(logger)
    if _FILE_LOGGING_OK is None:
        _FILE_LOGGING_OK = False
        logger.warning(
            "file logging unavailable under module_logging_path=%r; "
            "falling back to stderr stream logging",
            MODULE_LOGGING_PATH,
        )


# Configure one application namespace sink. Module loggers are children of this
# logger, so their record names remain searchable without opening handlers per
# module or emitting each record more than once.
_application_logger = logging.getLogger(MODULE_APP_NAME)
_configure_logger(_application_logger, _DEFAULT_LOGGER_NAME)

default_logger = logging.getLogger(f"{MODULE_APP_NAME}.{_DEFAULT_LOGGER_NAME}")
default_logger.setLevel(logging.NOTSET)
default_logger.propagate = True

_LOGGER_CACHE: dict[str, Logger] = {
    _application_logger.name: _application_logger,
    default_logger.name: default_logger,
}


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a module-named logger routed through the application handlers.

    A non-empty name becomes a child of the application logger. Existing fully
    qualified application module names are preserved; short names are qualified
    under ``MODULE_APP_NAME``. The application namespace owns the handlers, so
    sink configuration happens once while each module keeps a distinct logger.

    :param name: module logger name (``None`` / empty → the default logger).
    :return: a logger whose records propagate to the configured application sink.
    """
    requested_name = str(name or "").strip()
    if not requested_name:
        cache_key = default_logger.name
    elif requested_name == MODULE_APP_NAME or requested_name.startswith(
        f"{MODULE_APP_NAME}."
    ):
        cache_key = requested_name
    else:
        cache_key = f"{MODULE_APP_NAME}.{requested_name}"

    cached = _LOGGER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    logger = logging.getLogger(cache_key)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    _LOGGER_CACHE[cache_key] = logger
    return logger
