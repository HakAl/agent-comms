#!/bin/bash
exec "${AGENT_COMMS_PYTHON:-python3}" -m agent_comms.hooks.session_start
