"""Test package.

Importing it moves the process into a scratch home before any ``agent_comms``
module loads; see ``tests/isolation.py`` for what is redirected and how an
operator opts out for login-backed cell runs.
"""

from . import isolation as _isolation

_isolation.activate()
