# Python Environment

Use the `.venv` virtual environment for all Python development in this project. Install dependencies only into `.venv`, never into the system Python.

# Language

Create all generated artifacts in English unless explicitly asked otherwise.

# Secrets

Never expose private data from environment files in the context window, whether by directly reading files, using commands such as `cat`, `head`, `tail`, or `grep`, or through scripts or any other extraction method.

# Design

Apply YAGNI and KISS: implement only what is needed, using the simplest clear solution.

# GitHub CLI

Run `gh` commands outside the sandbox.

# Local Bot Runtime

For local end-to-end tests, run `./scripts/manage.sh start` from the project root outside the sandbox. It reuses healthy shared GigaAM or starts it, then starts the Docker Compose bot with the current `.env`, including webhook registration.

`start` uses the published image by default. To test current local code, build `vorec-telegram-bot:local`, set `VOREC_BOT_IMAGE` to it and `VOREC_BOT_PULL_POLICY=never` in `.env`, then use `manage.sh start`.

Before starting, verify required local configuration is present without exposing its values. For a test deployment, `COMPOSE_PROJECT_NAME` and `WEBHOOK_DOCKER_ALIAS` must differ from production; the alias must end in `-test`, and `WEBHOOK_PATH` must contain it. Never start if either value matches production. Do not use `docker run`, invoke Compose directly, or replace the configured runtime with synthetic settings. Do not run `manage.sh stop all` without explicit user approval.
