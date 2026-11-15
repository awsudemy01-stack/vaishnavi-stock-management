Prompt assets (code, not data):

- `core.md` — the locked guardrail core. Read-only for the Owner; its SHA-256 is recorded on every
  `prompt_version` row so a changed core is visible in the audit trail.
- `profile_staff.md` / `profile_owner.md` — the role section chosen from the **server-side** session role.
- `__init__.py` — `core_text()`, `core_hash()`, `profile(role)`, `build_system_prompt(...)` which composes
  core → role profile → Owner instructions (→ tool hint for providers without native tool calling).

Refusal templates live in `ai/guardrails.py` (they must not be editable), fast-path answer templates in
`ai/fastpath.py`, and the Owner's instructions in the database (`prompt_version`, edited in
*Settings → Assistant*).
