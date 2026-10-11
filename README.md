# SynAI — Self-Hosted Distributed AI Coding Platform

SynAI 2.0 is a web-first Ollama coding platform with FastAPI, a React/TypeScript Slate Dark interface, a Linux Client Agent and a restricted Sandbox Broker.

## Current capabilities

- Browser Chat with authenticated streaming, conversation persistence and no tool execution.
- Command Center, Workbench, projects, device status and immutable snapshots.
- `synai-client`: locally authorized Linux workspace snapshots with per-upload consent.
- `synai-broker`: isolated execution service, subject to Phase 13F-A runtime security acceptance.

**Browser-driven coding tasks and client-side patch application remain future phases.** They are not enabled by installing the Broker.

## Installation and development

Use Python 3.12+ in an isolated environment and run `python -m pip install -e .`. Supported Python entry points: `synai-web`, `synai-client`, and `synai-broker`. For the frontend, use `cd web && npm ci`, then follow `web/package.json` scripts. Configure credentials and data roots before starting services; see the dedicated phase guides.

## Architecture and safety

Core is the control plane and has no arbitrary host execution or general filesystem mutation authority. The Client Agent enforces local workspace authorization and per-capture upload consent. Broker execution occurs only inside approved, task-scoped containers under a separate service identity. Do not expose a container runtime socket to Core.

Read [architecture](docs/architecture/synai-2.0.md), [trust boundaries](docs/architecture/trust-boundaries.md), [protocols](docs/architecture/protocols.md), [Client Agent guide](docs/phase-13e-client-agent.md), and [Broker guide](docs/phase-13f-a-sandbox-broker.md).

## Tests

`python -m unittest discover -s tests`; frontend scripts live in `web/package.json`. Browser integration instructions are in `docs/phase-13d-web-chat.md`.

Do not delete private conversations, memory databases or snapshot data when changing installations. Apache-2.0; see LICENSE and NOTICE.
