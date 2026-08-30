"""Where the GUI's Help link points, and how it decides.

The README a user needs is the one that describes *their* build: someone running an
old package should not be reading instructions for a GUI they do not have. GitHub can
serve exactly that -- ``blob/v<version>/README.md`` -- but only if the tag exists, and
a version that has never been tagged (a source checkout mid-development, or a build cut
from an unreleased bump) would send them to a 404.

Nothing offline can tell the two cases apart, so the tag is *checked*, not assumed:
one HEAD request, on a background thread at startup, and the link is upgraded from the
branch README to the tagged one only if the tag really is there. Until then -- and
forever, if the machine is offline -- the link still works, it just points at the
latest README. That ordering matters: the fallback is the state the link starts in,
never something a failed lookup has to recover to.
"""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable

from .. import PROJECT_URL, __version__

logger = logging.getLogger(__name__)

#: Deliberately short. This runs at startup on a thread nobody waits for, so a slow
#: or captive network must not keep a thread alive behind the whole session; missing
#: the upgrade costs the user nothing but a less specific README.
_TIMEOUT_SECONDS = 3.0

#: The README of the default branch: always exists, always current.
LATEST_README_URL = f"{PROJECT_URL}#readme"


def tagged_readme_url(version: str = __version__) -> str:
    """The README as it stood at the ``v<version>`` release tag."""
    return f"{PROJECT_URL}/blob/v{version}/README.md"


def tag_exists(version: str = __version__, *, timeout: float = _TIMEOUT_SECONDS) -> bool:
    """Whether ``v<version>`` is a real tag on the repository.

    Any failure -- offline, DNS, proxy, rate limit, 404 -- answers False, because the
    caller's fallback is a URL that is always valid anyway.
    """
    url = f"{PROJECT_URL}/tree/v{version}"
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("tag lookup for v%s failed: %s", version, exc)
        return False


def resolve_readme_url(version: str = __version__) -> str:
    """The best README URL available for this build, checking the network once."""
    if tag_exists(version):
        return tagged_readme_url(version)
    return LATEST_README_URL


def resolve_readme_url_async(
    on_resolved: Callable[[str], None], version: str = __version__
) -> threading.Thread:
    """Run :func:`resolve_readme_url` off the event loop and hand back the result.

    A plain ``threading.Thread``, not a ``QThread`` -- see the note in ``workers.py``.
    It is a daemon: the answer is a nicety, and a hung request must never delay
    closing the window.
    """

    def run() -> None:
        on_resolved(resolve_readme_url(version))

    thread = threading.Thread(target=run, name="help-link-resolve", daemon=True)
    thread.start()
    return thread
