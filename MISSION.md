# Mission: Self-Rebuild Starter Kit

## End goal
Build and prove a safe, reproducible AI Orchestrator Starter Kit that installs from a bare Ubuntu VM with a simple Windows entry point and survives reboot.

## Required profiles
- Claude: Brain, Worker, Reviewer can operate with Anthropic tooling.
- OpenAI: Brain, Worker, Reviewer can operate without a hidden Claude dependency.
- Hybrid: explicit mixed-provider routing is supported.

## Clean-room proof
VM1 is reference material only. VM2 is the examiner. A successful run must install on clean VM2, pass diagnostics, survive reboot, execute a real Brain -> Worker -> Tests/Evidence -> Reviewer -> Repair when required -> Commit/Push flow, then pass a second clean installation after any discovered repair has been encoded in the kit.

## Autonomous authority
Diagnose technical failures, edit the Starter Kit, run non-destructive tests, retry, repair, and choose implementation details without asking the user. A technical error is not itself a NEEDS_HUMAN reason.

## NEEDS_HUMAN only
Interactive account login or 2FA, unavailable credential/API key, cloud control-plane action that deletes/rebuilds infrastructure, or a genuinely irreversible/destructive decision outside disposable test scope.

## Hard safety boundaries
Never copy credentials, tokens, login state, histories, customer data, internal business data, private worktrees, current cron configuration, or project logs from VM1. Never modify the production orchestrator as part of this mission. Never delete/rebuild a cloud VM without explicit approval.

## Definition of done
No undocumented manual technical repair is needed. Preflight is fail-closed. Both provider profiles are genuinely role-routable. Systemd/watchdog/monitor are healthy after reboot. Acceptance evidence and limitations are recorded. The final clean-room run passes from the documented entry point.
