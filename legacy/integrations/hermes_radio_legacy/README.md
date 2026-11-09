# hermes_radio_legacy

> **Not on the live path — reference only. See https://github.com/pcorajr/project-athena/blob/dev/docs/architecture/legacy.md**

Moved out of `integrations/hermes_radio/` by REL-5. The live Eye adapter no longer imports anything here.

Contents: `controller`, `tools`, `openai_provider`, `context`, `ownship`, `projections`, `failures`, `tactical`, `facts`, `hermes_client`, `adapter` (old `Adapter`), `simple_legacy` (`LegacySimpleController`, State Service picture), test-support `fixture_server`, `fixture_data`, `stream_fixture`, `text_smoke`, and `tests/`.

Requires `athena` and `metis` (State Service client) on `PYTHONPATH` — the retired State Service is not running.

```
export PYTHONPATH=<project-athena>/src:<gateway root>
python -m legacy.integrations.hermes_radio_legacy --state-client-config FILE   # legacy adapter
python -m legacy.integrations.hermes_radio_legacy.fixture_server --port 0 --scenario current_state
python -m pytest legacy            # 126 tests, run from the gateway root
```
