"""Source-free standard logging for the isolated self-hosted runtime.

The application owns the default Python/framework output of its processes, not
operator-added handlers, external database/platform logs, or native crash dumps.
No arbitrary log message, URL, response, stack or exception text is rendered.
Domain diagnostics printed through source_log keep their separate finite schema.
"""
from __future__ import annotations

import logging
import sys
import threading

from deployment_profile import selected_deployment_profile
from operational_copy_policy import quota_label


_HTTP_LOGGERS = ("httpx", "httpcore", "urllib3", "requests")
_COMPONENTS = (
    ("uvicorn", "asgi"),
    ("httpx", "http_client"), ("httpcore", "http_client"),
    ("urllib3", "http_client"), ("requests", "http_client"),
    ("psycopg", "database_client"), ("asyncio", "async_runtime"),
    ("self_hosted.process", "process"), ("self_hosted.thread", "thread"),
    ("self_hosted.unraisable", "unraisable"),
)


def _component(name):
    if isinstance(name, str):
        for prefix, value in _COMPONENTS:
            if name == prefix or name.startswith(prefix + "."):
                return value
    return "application"


def _error_type(exc_info):
    if isinstance(exc_info, tuple) and len(exc_info) >= 2:
        error = exc_info[1]
        if isinstance(error, BaseException):
            return quota_label("error_type", type(error).__name__)
    return None


class _SourceFreeLogRecord(logging.LogRecord):
    def getMessage(self):
        # Uvicorn's colour formatter can replace msg with extra.color_message.
        # Ignore that alternate message too; never stringify original arguments.
        return self._source_free_message


def _source_free_record_factory(name, level, pathname, lineno, msg, args,
                                exc_info, func=None, sinfo=None, **kwargs):
    severity = (
        logging.CRITICAL if level >= logging.CRITICAL else
        logging.ERROR if level >= logging.ERROR else
        logging.WARNING if level >= logging.WARNING else
        logging.INFO if level >= logging.INFO else logging.DEBUG
    )
    event = {
        logging.CRITICAL: "critical", logging.ERROR: "error",
        logging.WARNING: "warning", logging.INFO: "information",
        logging.DEBUG: "debug",
    }[severity]
    component = _component(name)
    message = "[framework] component=" + component + " event=" + event
    error_type = _error_type(exc_info)
    if error_type is not None:
        message += " error_type=" + error_type
    record = _SourceFreeLogRecord(
        "self_hosted." + component, severity, "", 0, message, (), None,
        func="", sinfo=None,
    )
    record._source_free_message = message
    # Access logging is disabled at installation. Keep records safe even if an
    # existing access formatter is called directly or the logger is re-enabled.
    if name == "uvicorn.access":
        record.args = ("[omitted]", "OTHER", "[omitted]", "1.1", 500)
    return record


def _unhandled_exception(exc_type, exc_value, traceback):
    logging.getLogger("self_hosted.process").error(
        "uncaught_exception", exc_info=(exc_type, exc_value, traceback),
    )


def _unhandled_thread_exception(arguments):
    if arguments.exc_type is SystemExit:
        return
    logging.getLogger("self_hosted.thread").error(
        "uncaught_thread_exception",
        exc_info=(arguments.exc_type, arguments.exc_value, arguments.exc_traceback),
    )


def _unraisable_exception(arguments):
    logging.getLogger("self_hosted.unraisable").error(
        "unraisable_exception",
        exc_info=(arguments.exc_type, arguments.exc_value, arguments.exc_traceback),
    )


def install_self_hosted_framework_logging() -> bool:
    """Install once per process after profile loading; private is a strict no-op.

    A record factory applies to descendant loggers and future standard handlers;
    logger-parent filters would not. It also closes preformatted lifespan errors,
    which carry their traceback inside msg rather than only exc_info.
    """
    if selected_deployment_profile() != "self-hosted":
        return False
    logging.setLogRecordFactory(_source_free_record_factory)
    logging.getLogger("uvicorn.access").disabled = True
    for name in _HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    sys.excepthook = _unhandled_exception
    threading.excepthook = _unhandled_thread_exception
    sys.unraisablehook = _unraisable_exception
    return True
