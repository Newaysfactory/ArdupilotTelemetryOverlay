"""Burn ArduPilot telemetry onto flight video as an FPV-style HUD/OSD overlay."""

#: The single place the version number is written. ``pyproject.toml`` reads it from
#: here (``[tool.setuptools.dynamic]``) instead of carrying its own copy, and the
#: release workflow refuses to build a ``v*`` tag that does not match it -- so the
#: tag on GitHub, the installed package and the number shown in the GUI cannot drift
#: apart. Bump it here, commit, then tag ``v<same number>``.
__version__ = "0.1.0"
