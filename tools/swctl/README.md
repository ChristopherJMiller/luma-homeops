# swctl

The TEG-30284 core switch as code. Runbook and rules: `switch/README.md`.
Design: `docs/superpowers/specs/2026-10-11-core-switch-as-code-design.md`.

- Run: `swctl …` inside `nix develop`. Every switch operation runs on the router
  (`chris@192.168.0.1`, SSH key in the agent), the only host that reaches the
  switch's management address (192.168.99.2, via the router's eth3).
- Test: `nix develop --command python -m pytest -q tools/swctl`
- Credentials: `switch/credentials.secret.yaml` (git-crypt), written to a
  private temp `snmp.conf` on the router per call; never on a command line.
- `tests/fakes.py` is a switch that stores what the real one stores and
  interprets the same SNMP writes; `apply` is tested end-to-end against it.
