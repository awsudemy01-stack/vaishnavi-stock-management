# Tests

| Folder | Runs with | Needs |
|--------|-----------|-------|
| `tests/core/` | `python -m unittest discover -s tests/core -t .` | bare Python 3.11+ only — no packages |

One tier, on purpose: every test runs on a plain interpreter, which is why the same folder **ships with the
application** — `python main.py --selftest` executes it on the shop PC to prove the installation, and
`main.py --update` runs it to decide whether a new release is kept or rolled back. Do not remove it from a
release. `tests/core/golden/` holds the SHA-256 of the reference receipts; delete those two files and run
`test_receipts` twice when the receipt layout changes deliberately.

Run from the project root; add `$env:PYTHONIOENCODING="utf-8"` on a Windows console that is not UTF-8.
