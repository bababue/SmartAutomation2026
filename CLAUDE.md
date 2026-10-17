@AGENTS.md

# Claude Code specifics

`AGENTS.md` above is the canonical source. This file holds only what is specific to Claude Code.

- Start dev servers through `.claude/launch.json` (`api`, `api-demo`, `web`), not from the shell.
  Check what is already running first.
- For a second opinion on larger changes, use the Codex plugin (`codex:codex-rescue` agent, or
  `/codex:review`, `/codex:adversarial-review`). `/code-review` is for correctness.
- Before a turn ends with changed code, run the gates from AGENTS.md → Quality gates
  (`pytest`; for `web/`, `npm run lint`, `npm test` and `npm run build`).
- Confidentiality (AGENTS.md) also covers artifacts, memory files and agent prompts: no dataset
  values in anything that is published or shared.
