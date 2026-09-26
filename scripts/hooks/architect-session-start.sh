#!/bin/bash
# Runs the session-start hook on the interpreter that owns the agent-comms
# install. The seat launcher exports AGENT_COMMS_INSTALL_ROOT as the install's
# sys.prefix (the .venv of a checkout, the tool venv of an installed package),
# so that prefix's python is preferred; AGENT_COMMS_PYTHON overrides it (tests
# point it at a stub), and python3 on PATH is the last resort.
python="${AGENT_COMMS_PYTHON:-}"
if [ -z "$python" ] && [ -n "${AGENT_COMMS_INSTALL_ROOT:-}" ] && [ -x "$AGENT_COMMS_INSTALL_ROOT/bin/python" ]; then
  python="$AGENT_COMMS_INSTALL_ROOT/bin/python"
fi
exec "${python:-python3}" -m agent_comms.hooks.session_start
