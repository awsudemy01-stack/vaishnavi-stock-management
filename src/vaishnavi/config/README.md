# `config/` — editable configuration templates

| File | Purpose | Added in unit |
|------|---------|---------------|
| `settings.toml` | shop profile, tax, receipt, AI provider/models, paths, server | core/settings |
| `logging.toml` | handlers, rotation, levels per module | core/logging |
| `alerts.toml` | alert-rule thresholds → notification centre | services/notifications |
| `festivals.toml` | festival calendar for forecasting | forecast/festivals |
| `install-answers.toml` | non-secret answers of the interactive installer (generated) | bootstrap/wizard |

**Never put an API key or password in any file here** — the app refuses to start (`E-SEC-006`).
Keys are pasted in the installer / *Settings → Assistant* and stored encrypted.
