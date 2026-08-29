# Python Environment

Use the `.venv` virtual environment for all Python development in this project. Install dependencies only into `.venv`, never into the system Python.

# Language

Create all generated artifacts in English unless explicitly asked otherwise.

# Secrets

Never expose private data from environment files in the context window, whether by directly reading files, using commands such as `cat`, `head`, `tail`, or `grep`, or through scripts or any other extraction method.

# Design

Apply YAGNI and KISS: implement only what is needed, using the simplest clear solution.

# Local Bot Runtime

For local end-to-end tests, run `./scripts/manage.sh start` from the project root outside the sandbox. It starts native GigaAM and the Docker Compose bot with the current `.env`, including webhook registration.

Before starting, verify required local configuration is present without exposing its values. For a test deployment, `WEBHOOK_DOCKER_ALIAS` must differ from production and end in `-test`; `WEBHOOK_PATH` must contain that exact alias. Never start if either value matches production. Do not use `docker run`, invoke Compose directly, or replace the configured runtime with synthetic settings.
