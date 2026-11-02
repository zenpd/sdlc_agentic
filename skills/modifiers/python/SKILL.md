---
name: python
description: Stack notes for Python repos - follow the existing style, keep dependencies unchanged unless the ticket says otherwise.
license: MIT
metadata:
  detect_files: "pyproject.toml,requirements.txt,setup.py,Pipfile"
  variables: "TICKET_KEY,TICKET_SUMMARY,TARGET_REPO,TARGET_BRANCH,PERSONA"
---
This repository uses Python. Apply these notes on top of your persona:

- Match the existing code style: look at neighbouring files (naming, imports, docstrings, type hints) and copy their conventions.
- Check `pyproject.toml`, `setup.cfg` or `tox.ini` for the formatter/linter settings (line length, quote style) and respect them.
- Do not add or upgrade dependencies unless the ticket explicitly asks for it. If it does, edit the dependency file (`requirements.txt` / `pyproject.toml`) only - never install anything.
- Update relevant tests and run the narrowest existing test or check when practical.