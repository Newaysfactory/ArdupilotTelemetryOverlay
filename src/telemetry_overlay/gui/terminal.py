"""A read-only log view that behaves like a real terminal for ``\\r``-updated lines.

``cli.py``'s ``_progress_bar`` writes a progress bar in place with a bare ``\\r``
(no ``\\n``) the way a real terminal would redraw it. A plain ``QPlainTextEdit``
has no notion of "carriage return": appending that text verbatim would either
show a stray character or scroll one full block per update instead of one
updating line. ``TerminalWidget`` interprets ``\\r`` itself so redirected CLI
output looks the same here as it does in a real terminal.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
from collections.abc import Iterator

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

_SPLIT = re.compile(r"(\r|\n)")


class TerminalWidget(QPlainTextEdit):
    """Read-only output pane shared by every command the GUI runs."""

    #: Emitted from any thread; queued to the GUI thread by the connection below.
    _append_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        self._append_requested.connect(
            self._on_append, Qt.ConnectionType.QueuedConnection
        )

    def write(self, text: str) -> None:
        """Thread-safe entry point: safe to call from a worker thread."""
        if not text:
            return
        try:
            self._append_requested.emit(text)
        except RuntimeError:
            # The widget was already destroyed (window closed while a background
            # command was still writing to it) -- nothing left to append to.
            pass

    def _on_append(self, text: str) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        for chunk in _SPLIT.split(text):
            if not chunk:
                continue
            elif chunk == "\n":
                cursor.insertText("\n")
            elif chunk == "\r":
                # Erase back to the start of the current line; the next chunk
                # then types over it, exactly like a real terminal's CR.
                cursor.movePosition(
                    QTextCursor.MoveOperation.StartOfBlock,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                cursor.removeSelectedText()
            else:
                cursor.insertText(chunk)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()


class _StreamRedirector(io.TextIOBase):
    """A file-like object that forwards ``write()`` to a :class:`TerminalWidget`."""

    def __init__(self, widget: TerminalWidget) -> None:
        self._widget = widget

    def write(self, text: str) -> int:
        self._widget.write(text)
        return len(text)

    def flush(self) -> None:  # pragma: no cover - nothing to flush
        pass


@contextlib.contextmanager
def redirect_to_terminal(widget: TerminalWidget) -> Iterator[None]:
    """Redirect ``sys.stdout`` to ``widget`` for the duration of the block.

    Only one command runs at a time (see ``workers.CommandWorker``), so mutating
    the process-global ``sys.stdout`` for the call's duration is safe.
    """
    previous = sys.stdout
    sys.stdout = _StreamRedirector(widget)  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.stdout = previous
