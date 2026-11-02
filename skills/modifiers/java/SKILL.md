---
name: java
description: Stack notes for Java repos - follow the existing package layout and build file, no new dependencies unless asked.
license: MIT
metadata:
  detect_files: "pom.xml,build.gradle,build.gradle.kts"
  variables: "TICKET_KEY,TICKET_SUMMARY,TARGET_REPO,TARGET_BRANCH,PERSONA"
---
This repository uses Java. Apply these notes on top of your persona:

- Follow the existing package structure and naming; put new classes next to the ones they relate to.
- Match the code style already in the repo (braces, annotations, Javadoc habits) rather than introducing your own.
- Do not add dependencies to `pom.xml` / `build.gradle` unless the ticket asks for it.
- Update relevant tests and run the narrowest existing test or build check when practical.