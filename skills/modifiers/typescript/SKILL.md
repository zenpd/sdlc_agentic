---
name: typescript
description: Stack notes for TypeScript repos - keep types strict, follow existing component and import patterns.
license: MIT
metadata:
  detect_files: "tsconfig.json"
  variables: "TICKET_KEY,TICKET_SUMMARY,TARGET_REPO,TARGET_BRANCH,PERSONA"
---
This repository uses TypeScript. Apply these notes on top of your persona:

- Keep types explicit and strict. Do not use `any` or `// @ts-ignore` to make something compile; fix the type instead.
- Follow the existing import style (path aliases such as `@/...`, named vs default exports) by looking at nearby files.
- Reuse existing helpers, hooks and components before writing new ones.
- Do not add or upgrade packages unless the ticket asks for it. Update relevant tests and run the narrowest existing check when practical.