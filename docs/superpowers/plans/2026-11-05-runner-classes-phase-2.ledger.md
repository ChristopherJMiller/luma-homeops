# Runner classes phase 2: ledger

Plan: `2026-11-05-runner-classes-phase-2.md`. Controller's SDD notes live in `.superpowers/sdd/2026-11-05-runner-classes-phase-2/progress.md`. This file holds no secret, no Azure id and no hidden org, team or group name.

Phase 1 ledger cited below: `.superpowers/sdd/2026-11-04-runner-classes-phase-0-1/progress.md` (lines as of 2026-11-05).

## Answers

### Chris (2026-11-05)

- Phase 2 ships everyone at once (price feed, paid accounting, member access together). It starts in parallel with phase 1. Chris applies all terraform.
- Spot is unavailable on this subscription (phase 1 ledger line 16).
- Decided: `realliance` limit $30, other owners $10; the forge fork builds on `medium`; held undos back off to 60 min after 6.
- Plan approved: group (a) Tasks 0-9 now; group (b) after phase 1 Task 14; group (c) after phase 1 Task 18.
- F1: a paid held undo after compute existed charges `CostOf(compute_at -> stop)`, settles on absence, no try. `ErrNoCapacity` stays 0.
- F2-F39 adopt the scan's proposed ruling, first option where one is offered.
- Cache design: paid CI cap $80 per period (CI-group budget $85); release repo `realliance/forgejo` first; per-job egress cap in phase 2.
- Accepted risk: a compromised dispatcher churning groups shorter than one sweep evades the egress rule; only the budget bounds it. Task 20 records it as spec amendment (h).

### Carried phase 1 rulings (phase 1 `progress.md` line)

- 4: credit period is the UTC calendar month; ledger is `modernc.org/sqlite`.
- 16: Spot unavailable; VM classes pay-as-you-go only.
- 20: concurrency-held jobs hold and back off 1/2/5/10 min; paid VM classes revisit in phase 2.
- 24: bots live in the Owners-only `realliance-ops` org.
- 40: enforcement keys on numeric ids; names are labels.
- 41: store write equals dispatcher compromise; code ceilings bound spend.
- 45: reservation checks owner and global budgets; a 0 global means no paid.
- 54-55: `DeleteRunner(ctx, id, uuid)` reads first and refuses non-ours; guard checks the POST body.
- 61, 68-69: http.Client literals allowed in `guarded`; Job POST guard rules; fail closed on token faults.
- 81, 90-91: `Reserve` assigns Try; ledger caps and refuses negatives; `Tries` excludes held undos.
- 117-119: fair share by minutes used; paid-gate stub is replaced, never deleted (line 119).
- 144, 156, 158: step budget 20 s; the reaper name grammar covers `ci-tiny-` only, so phase 2 extends `ownName`.
- 154: Task 18 precondition N1 relaxed; Task 19 keeps the full gate.
- 174-178, 185: fix-round rulings (verify at now first, back-off after 6 held undos grows to 60 min).
- Carry (line 175): M6 settle on observed absence goes to phase 2.
- Carry (line 82): hold history grows unbounded; "revisit in phase 2" (see open items).

### Pre-flight rulings

F1 Chris; F2-F39 adopted. Table is in the plan under "Pre-flight rulings applied".

### Open items for Chris (F36), raised at Task 16's review

Two phase 1 carries address phase 2 and this plan does not claim them:

1. Phase 1 Task 12 fix round: "M8 -> Chris at Task 19/phase 2" (phase 1 ledger line 147).
2. The hold table's growth: soft-cleared rows are never pruned, "revisit in phase 2" (phase 1 ledger line 82).

## Baseline

Taken 2026-11-05.

- `az`: not logged in (the CLI is not on PATH in this shell, so `az account show` could not run). Expected `Realliance`, `Enabled`; recheck before Task 4.
- Phase 1 Task 13: complete.
- Phase 1 Task 14: at Step 2. Chris merges ci-dispatch PR #1 and tags `v0.1.0`. `git ls-remote --tags origin v0.1.0` on ci-dispatch printed nothing, so the tag does not exist yet.
- Phase 1 Task 18: not started.
- forge-bot Task 15: not complete. Its ledger has no Task 15 report, only carries addressed to it (add `ForgeBotOwnersUnknown` and `ForgeBotSelfCheckBlind` to the `forge-urgent` route). `forge-urgent` appears nowhere in `cluster/` on main, only in plans, so the route does not exist yet.
- NetworkPolicy plan: Task 6 (B1a) complete (`B1a_at=2026-11-05T21:21:17Z`). Task 7 (B1b) is running now; its 24 h soak follows, with Gate G excluding ci-dispatch and ci-tiny.
- Worktrees: `~/Repos/luma-homeops-ci2` (`ci-phase2`, from `origin/main` 31c25fcc) and `~/Repos/ci-dispatch-v2` (`v2`, from `origin/v1` 9055e60), both clean.

## Task 1

## Task 2

## Task 3

## Task 4

## Task 5

## Task 6

## Task 7

## Task 8

## Task 9

## Task 10

## Task 11

## Task 12

## Task 13

## Task 14

## Task 15

## Task 16

## Task 17

## Task 18

## Task 19

## Task 20

## Task 21

## Task 22
