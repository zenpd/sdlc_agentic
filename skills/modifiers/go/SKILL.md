---
name: go
description: Stack notes for Go repos - idiomatic error handling, gofmt style, no new modules unless asked.
license: MIT
metadata:
  detect_files: "go.mod,go.work"
  variables: "TICKET_KEY,TICKET_SUMMARY,TARGET_REPO,TARGET_BRANCH,PERSONA"
---
This repository uses Go. Apply these notes on top of your persona:

- Write gofmt-style code (tabs, standard import grouping) and keep exported names documented the way neighbouring code does.
- Handle every error explicitly and return it with context (for example with `fmt.Errorf("...: %w", err)`); do not ignore errors.
- Keep changes inside the existing package layout. Do not add modules to `go.mod` unless the ticket asks for it.
- Update relevant tests and run the narrowest existing Go test or check when practical.