# hactl

Home Assistant agent toolkit for galaxy: query, screenshot, preview, lint,
health-check and deploy-verify HA. Runbook and command table: `docs/ha.md`.
Workflow: the `ha-config` skill.

- Run: `hactl …` inside `nix develop` (the dev shell puts `bin/` and hactl's
  python first on PATH and points Playwright at the nix-built browsers).
- Test: `nix develop --command python -m pytest -q tools/hactl`
- Token: `$HA_TOKEN`, else `agent.secret.yaml` (git-crypt). Never printed.
- Layout: one module per command group in `src/hactl/`; each exposes
  `register(subparsers)` and is listed in `__main__.MODULES`. Heavy
  dependencies (websockets, playwright) are imported inside functions so CI
  needs only pyyaml + pytest.
- The in-cluster reload hook is `cluster/home-assistant/hooks/ha_reload.py`
  (stdlib only); its tests live here in `tests/test_ha_reload.py`.
