# legacy/

> **Not on the live path — reference only. See https://github.com/pcorajr/project-athena/blob/dev/docs/architecture/legacy.md**

Python code retired from the live Athena Eye path, kept (via `git mv`, history intact) for reference.
Go legacy lanes (upstream SkyEye GCI stack) stay in place; see `ATHENA.md`.

- `integrations/hermes_radio_legacy/` — State Service era adapter, controller, tools, fixtures and their tests.

Run the legacy tests on demand (gateway repo root, project-athena `legacy/src` (has `athena`, full `metis`) and `src` on the path):

```
PYTHONPATH=<project-athena>/legacy/src:<project-athena>/src:. python -m pytest legacy
```
