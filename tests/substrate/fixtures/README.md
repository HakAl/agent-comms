# Substrate fixtures

Recorded outputs that the substrate tests compare against byte for byte.

## Setup and doctor snapshots

`runtime_setup_doctor_snapshot.json` and `runtime_setup_doctor_fake_snapshot.json`
are what `agent-comms setup` and the doctor report for a scratch project with
fake clients (`tests/substrate/test_runtime_protocol.py`, `SetupDoctorIdentityTest`).
Host-specific values are replaced before the comparison: the runtime root,
the scratch root and the interpreter's bin directory become placeholders, and
the platform key is pinned to `SNAPSHOT_PLATFORM` (`darwin-arm64`) so the
snapshots compare on any host. The certified versions inside them come from
`agent_comms/runtime_pins.json`.

Regenerate after an intended change to setup, doctor or the pins, then read the
diff before committing it:

```sh
AGENT_COMMS_TEST_WRITE_SNAPSHOTS=1 uv run --frozen --extra test \
  python -m unittest tests.substrate.test_runtime_protocol -k SetupDoctorIdentityTest
git diff tests/substrate/fixtures/
```

The switch rewrites both files from the current run and then compares, so the
run passes by construction; the diff is the review. The isolation guard keeps
`AGENT_COMMS_TEST_*` variables, so the switch reaches the test process.

## Other fixtures

`certified_spawn_v2.json` and `runtime_render_snapshot.json` are maintained by
the tests that read them (`tests/substrate/test_spawn.py`,
`tests/substrate/test_runtime_protocol.py`); see the comments there.
