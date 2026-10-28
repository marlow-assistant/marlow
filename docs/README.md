# docs

The record of how Marlow was built: written by and for the Claude Code
sessions that built it, so it reads more like a lab notebook than a manual.
To install Marlow, see [`deploy/INSTALL.md`](../deploy/INSTALL.md) instead.

"v2" is the current design. Version 1 was Claude Code running interactively
in a long-lived terminal session, driven over chat; v2 replaced that with
the server and apps in this repository.

Where to start:

- [`marlow-v2-design.md`](marlow-v2-design.md): why it is built the way it
  is, with the options that were turned down left in.
- [`marlow-v2-implementation-plan.md`](marlow-v2-implementation-plan.md):
  what was built, module by module.
- [`marlow-v2-START.md`](marlow-v2-START.md): where things stand now.
- [`tasks/`](tasks/): one note per feature, bug or decision, each with its
  reasoning.
