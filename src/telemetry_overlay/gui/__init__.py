"""PySide6 GUI: a control panel over the existing CLI commands.

Deliberately thin. Every button here calls straight into ``cli.py``'s ``cmd_*``
functions or the library functions they themselves call (``export_video``,
``compute_manual_diagnostics``, ...) -- no rendering, sync or export logic is
reimplemented here, only Qt orchestration around it.
"""
