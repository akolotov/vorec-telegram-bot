# Vorec Telegram Bot

Telegram bot that transcribes allowed users' voice messages and audio files.

![An Apple Watch with the X-Large face and a single Voice Memos complication](.assets/apple-watch-v8-voice-memo-faces.png)

## Why this exists

This bot turns quick spoken notes into reusable text. It gives an otherwise unused Apple Watch
Series 8 a second life as a dedicated voice recorder: its X-Large face has a single
Voice Memos complication, so recording an idea is a one-tap action.

The recording syncs from the watch to iCloud and then appears in Voice Memos on the iPhone. From
there it is shared to this Telegram bot, which returns a transcript. That text can be saved in
notes, turned into a GitHub issue, or used as material for a draft.

```text
Apple Watch → Voice Memos → iCloud → iPhone → Share to Telegram bot → transcript
```

The setup is especially useful for Russian-language recordings: the built-in transcription
available in the iPhone Voice Memos workflow does not cover this use case, so the bot combines
Russian-focused and general-purpose speech recognition instead.

The bot is intended primarily for Russian-language audio. Its main transcription comes
from a configurable OpenAI-compatible provider, currently Whisper through oMLX.
GigaAM provides a second, Russian-focused transcription and runs locally on Apple Silicon.

## Components

```text
Telegram → Tailscale Funnel → tailscale-ingress → bot container → GigaAM API on the Mac host
                       └→ OpenAI-compatible inference provider
```

- **Inference provider** produces the main transcription, currently with Whisper. Whisper
  can preserve English terms, anglicisms, and mixed Russian/English speech better in some
  recordings.
- **GigaAM** produces a second transcription using the existing MLX model in the oMLX
  cache. It is strong on Russian speech, but may transliterate English terms. One native GigaAM
  process can serve multiple bot deployments on the same Mac.
- **Merge model** receives both transcripts through the inference provider and combines
  their best-supported readings into one readable result. Both transcriptions are always
  made: the bot does not currently try to judge the quality of the first result.
- **oMLX** is the current local inference provider. It is not required by the architecture:
  configure any OpenAI-compatible provider with `INFERENCE_API_URL` and
  `INFERENCE_API_KEY`.
- **Docker** runs only the bot; MLX stays native on Apple Silicon.

Set `SMART_TRANSCRIPTION_SCHEDULING=true` only when Whisper and GigaAM have independent
capacity. In that mode, concurrent recordings can start with whichever ASR resource is available,
but each recording still uses only one ASR at a time and completes both before merge. The setting
defaults to `false`; its value must be `true` or `false`.

All configuration, including API URLs and tokens, is in `.env`. Start by copying `.env.example`.

## Webhook through Tailscale Funnel

The shared Tailscale gateway on this machine already exposes the generic
`/hooks/<docker-alias>/...` route. The Compose service reads its network alias
from `WEBHOOK_DOCKER_ALIAS`; it must match the `<docker-alias>` segment in
`WEBHOOK_PATH`.

This deployment depends on the shared
[tailscale-funnel-gateway](https://github.com/akolotov/tailscale-funnel-gateway).
Deploy that gateway first: it creates the external `tailscale-ingress` Docker
network and publishes the Funnel hostname used by `WEBHOOK_PUBLIC_BASE_URL`.

Set the matching values in `.env` before deployment. `WEBHOOK_SECRET_TOKEN`
must be a new random 1-256 character value using only letters, digits,
underscores, and hyphens; it is sent to Telegram during `setWebhook` and the
receiver rejects requests that do not include it.

Generate a suitable secret with the project's virtualenv, then copy its output
to `WEBHOOK_SECRET_TOKEN`:

```sh
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

```dotenv
WEBHOOK_PUBLIC_BASE_URL=https://<funnel-hostname>.<tailnet>.ts.net
WEBHOOK_DOCKER_ALIAS=<docker-alias>
WEBHOOK_PATH=/hooks/<docker-alias>/<webhook-endpoint>
WEBHOOK_SECRET_TOKEN=<new-random-secret>
```

Each deployment must also set a unique `COMPOSE_PROJECT_NAME`. Docker Compose uses this name to
keep containers from different checkouts separate. A second deployment needs a different
`TELEGRAM_BOT_TOKEN`, `COMPOSE_PROJECT_NAME`, `WEBHOOK_DOCKER_ALIAS`, and matching
`WEBHOOK_PATH`.

## Docker build

The project deliberately has two dependency files:

- `requirements.txt` is for the local macOS environment. It includes GigaAM and MLX, which run
  natively on Apple Silicon.
- `requirements.container.txt` is for the Linux bot container. It excludes GigaAM/MLX and
  includes only the bot's runtime dependencies, including `python-telegram-bot[webhooks]` for
  the HTTP webhook server.

Keeping them separate prevents Docker from trying to install macOS-only MLX dependencies. The
local GigaAM runtime requires an Apple Silicon Mac running macOS 14 or later and Python 3.12 or
3.13.
GitHub Actions builds and publishes the Linux image to GitHub Container Registry after changes
are merged into `main` and when version tags are pushed. `./scripts/manage.sh start` pulls the
latest published image through Docker Compose after GigaAM is ready; it does not build an image
locally. By default, Compose uses `ghcr.io/akolotov/vorec-telegram-bot:latest` and pulls it on
each start. Set `VOREC_BOT_IMAGE` in the shell or `.env` to run a specific published tag instead.
To run a local build, use `VOREC_BOT_IMAGE=vorec-telegram-bot:local` and
`VOREC_BOT_PULL_POLICY=never`.

## First setup

In a new project directory, create the local Python 3.12 environment and install the macOS
dependencies:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Configure `.env` with the Telegram token, allowed user IDs, API credentials, and unique webhook
settings. For a second deployment, use a different `TELEGRAM_BOT_TOKEN`,
`COMPOSE_PROJECT_NAME`, `WEBHOOK_DOCKER_ALIAS`, and matching `WEBHOOK_PATH`. Both deployments
can use the same healthy GigaAM process; configure the same `GIGAAM_URL`, `GIGAAM_API_KEY`, and
`GIGAAM_PORT` in both. Set `GIGAAM_MANAGER_SCRIPT` in a secondary deployment to the absolute path
of the checkout that owns that process.

## Persistent data

Docker Compose bind-mounts `./data` from the directory containing `docker-compose.yml` to
`/app/data` in the bot container. Each message is saved under its timestamp, chat ID, and Telegram
message ID (`YYYY-MM-DD_HH-MM-SS_<chat-id>_<message-id>`):

- `data/voices/YYYY-MM/<recording-id>.<extension>` contains the downloaded audio.
- `data/transcripts/YYYY-MM/<recording-id>/` contains the `whisper`, `gigaam`, and `merged`
  responses in both `.json` and `.txt` formats.

The intermediate converted WAV is deleted after processing. The `data/` directory is intentionally
excluded from Git. Incoming messages are handled concurrently, while the bot serializes each
ffmpeg, GigaAM, and oMLX stage and reports when a recording is waiting for one of them. Smart
transcription scheduling coordinates capacity only within one bot process; separate deployments
that share a provider must rely on that provider to enforce its own global capacity.

`manage.sh` does not install dependencies. It checks the local GigaAM health endpoint first. Only
the GigaAM manager checks for `.venv`, tmux, and the model directory when it needs to start a new
GigaAM process. Set `GIGAAM_MODEL_PATH` when the model is not stored in its default oMLX cache
location.

## Management

Use one script to manage both services:

```sh
./scripts/manage.sh start
./scripts/manage.sh stop
./scripts/manage.sh stop all
./scripts/manage.sh status
./scripts/manage.sh logs bot
./scripts/manage.sh logs gigaam
```

`start` checks the shared GigaAM health endpoint. It reuses a healthy process or starts GigaAM in
a detached tmux session, then starts the bot with Docker Compose and registers the Telegram
webhook. By default, the Compose service has `pull_policy: always`, so it checks for a new
published image on every start without building one locally. `stop` stops only this bot and leaves
shared GigaAM running. `stop all` stops this bot and then stops the shared GigaAM process; use it
only when no other deployment needs GigaAM.
