# Clean-room rules

The starter kit contains generic infrastructure only.

Never copy credentials, login state, API keys, tokens, private project data, histories, logs, worktrees, customer data, private repository policy, or project cron jobs from the reference VM.

Every manual fix found during VM2 testing must be encoded in the installer or documented as an unavoidable account-login step. A clean rebuild must pass without undocumented repair.
