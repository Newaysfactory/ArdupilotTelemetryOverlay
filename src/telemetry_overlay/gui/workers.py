"""Background execution for the CLI commands the GUI drives.

Every tab funnels its command through :class:`CommandWorker` instead of calling
``cmd_probe``/``cmd_autosync``/``export_video``/... directly on the GUI thread:
a real ``.bin`` takes seconds to parse and video analysis (autosync, manualsync)
or an export can take minutes, and none of that may block the event loop.

This deliberately runs the work on a plain :class:`threading.Thread`, not a
``QThread``. On Windows, a ``QThread`` running PyAV/FFmpeg work (``av.open()``
and anything that decodes) reliably crashes the process with an access
violation -- reproduced with nothing more than ``probe_video()`` called from a
``QThread.run()`` override, with or without any widgets involved; the same
call from a plain ``threading.Thread`` alongside a running Qt event loop never
crashes. This is presumably some incompatibility between Qt's native thread
creation on Windows and FFmpeg's internal thread pool, not anything specific
to this codebase. ``Signal`` delivery back to the GUI stays safe from a plain
thread: Qt's default "auto" connection compares the *actual calling thread*
against the receiving object's thread affinity at emit() time (not the
affinity of the object the signal is declared on), so a background
``threading.Thread`` calling ``.emit()`` is correctly delivered as a queued
call on the GUI thread without anything special at the connecting end.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal

from .terminal import TerminalWidget, redirect_to_terminal


class CommandWorker(QObject):
    """Runs one callable on a background thread with stdout captured to a terminal."""

    succeeded = Signal(object)
    failed = Signal(str)
    #: Emitted after succeeded/failed either way -- convenient for "done, stop tracking".
    finished = Signal()

    def __init__(
        self,
        func: Callable[[], Any],
        terminal: TerminalWidget,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._func = func
        self._terminal = terminal
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        with redirect_to_terminal(self._terminal):
            try:
                result = self._func()
            except Exception as exc:  # noqa: BLE001 - surfaced to the GUI, not swallowed
                print(f"error: {exc}")
                self.failed.emit(str(exc))
                self.finished.emit()
                return
        self.succeeded.emit(result)
        self.finished.emit()
