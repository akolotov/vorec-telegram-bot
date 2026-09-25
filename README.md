# Vorec Telegram Bot

Telegram bot that transcribes allowed users' voice messages and audio files. Its
read-only Telegram Mini App shows each user's completed transcripts by date or
personal tag.

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
two speech-recognition results instead.

The bot is intended primarily for Russian-language audio. Its primary and secondary
transcriptions come from configurable OpenAI-compatible providers, currently Whisper models
served through oMLX.

## Components

```text
Telegram → Tailscale Funnel → tailscale-ingress → bot container
                                                   ├→ primary inference provider
                                                   └→ secondary inference provider
```

- **Primary inference provider** produces the main transcription.
- **Secondary inference provider** produces an independent second transcription. It may use the
  same OpenAI-compatible endpoint as the primary provider with a different model.
- **Merge model** receives both transcripts through the primary inference provider and combines
  their best-supported readings into one readable result. Both transcriptions are always
  made: the bot does not currently try to judge the quality of the first result.
- **Title model** receives the merged transcript and the user's available tag names and
  descriptions through the primary inference provider. One structured response creates the
  short title and gives a true/false decision for every personal tag, with an explanation
  when none match. The optional `TITLE_MODEL` environment variable selects the model.
  After three unsuccessful attempts, the bot saves the transcript
  with its first 50 characters as the title and no tags.
- **oMLX** is the current local inference provider. It is not required by the architecture:
  configure any OpenAI-compatible providers with `INFERENCE_API_URL`, `INFERENCE_API_KEY`,
  `SECONDARY_INFERENCE_API_URL`, and `SECONDARY_INFERENCE_API_KEY`.
- **Docker** runs only the bot; MLX stays native on Apple Silicon.

Set `SMART_TRANSCRIPTION_SCHEDULING=true` only when the primary and secondary providers have
independent capacity. In that mode, concurrent recordings can start with whichever ASR resource
is available, but each recording still uses only one ASR at a time and completes both before
merge. The setting defaults to `false`; its value must be `true` or `false`.

All configuration, including API URLs and tokens, is in `.env`. Create it from the example and
then configure its values:

```sh
cp .env.example .env
```

## Webhook through Tailscale Funnel

The shared Tailscale gateway on this machine already exposes the generic
`/hooks/<docker-alias>/...` route. The Compose service reads its network alias
from `WEBHOOK_DOCKER_ALIAS`; it must match the `<docker-alias>` segment in
`WEBHOOK_PATH`.

This deployment depends on the shared
[tailscale-funnel-gateway](https://github.com/akolotov/tailscale-funnel-gateway).
Deploy that gateway first: it creates the external `tailscale-ingress` Docker
network and publishes the Funnel hostname used by `COMMON_PUBLIC_BASE_URL`.
Its existing `/apps/<docker-alias>/...` route also forwards the Mini App to this
container without a gateway configuration change.

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
COMMON_PUBLIC_BASE_URL=https://<funnel-hostname>.<tailnet>.ts.net
WEBHOOK_DOCKER_ALIAS=<docker-alias>
WEBHOOK_PATH=/hooks/<docker-alias>/<webhook-endpoint>
WEBHOOK_SECRET_TOKEN=<new-random-secret>
MINI_APP_PATH=/apps/<docker-alias>/
```

The Mini App URL is built from `COMMON_PUBLIC_BASE_URL` and `MINI_APP_PATH`;
the path must match `/apps/<WEBHOOK_DOCKER_ALIAS>/`. For existing deployments,
`WEBHOOK_PUBLIC_BASE_URL` remains a fallback when the common setting is absent,
and `MINI_APP_PATH` defaults to the alias-based route when omitted. The separate
`MINI_APP_PUBLIC_URL` setting is no longer used.

On startup, the bot sets a personal **Memos** menu button for every
allowed user. If Telegram rejects an initial attempt before a
user has opened the private chat, the bot retries when that user next sends a
private message.

## Memos Mini App

Open the bot's menu button in a private Telegram chat. The Mini App starts with
transcripts grouped by local date; switch to **By Categories** for greedy, disjoint
tag groups. Each title opens the complete text. Navigation labels are in
English, while dates and status messages remain in Russian. The UI adapts to
Telegram's light and dark themes.

The browser sends Telegram's raw `initData` on each read request. The server
checks its signature and one-hour lifetime, then reads only rows owned by the
signed Telegram user ID. Full transcript text is fetched only after opening a
title. The Mini App never edits transcripts or tags.

The HTTP server writes access logs for Mini App pages, assets, API requests,
and Telegram webhooks. Each entry contains the client address seen by the
server, the method, the path and query string, and the response status. It
does not contain request headers or response bodies. Keep these logs private:
detail paths contain transcript IDs, and list queries contain the timezone.

Tags are personal to a user. Their stored names are lowercase without `#`, and
the `#` prefix is added in the UI. Another process may populate the available tags;
the bot then assigns them to new transcripts. Until tags are available, the tag view
shows an **Uncategorized** group. A database upgrade from
schema version 2 expects both tag tables to be empty and stops safely if it
finds existing tag data.

Each deployment must also set a unique `COMPOSE_PROJECT_NAME`. Docker Compose uses this name to
keep containers from different checkouts separate. A second deployment needs a different
`TELEGRAM_BOT_TOKEN`, `COMPOSE_PROJECT_NAME`, `WEBHOOK_DOCKER_ALIAS`, and matching
`WEBHOOK_PATH`.

## Docker build

GitHub Actions builds and publishes the Linux image to GitHub Container Registry after changes
are merged into `main` and when version tags are pushed. `docker compose up -d` does not build an
image locally. By default, Compose uses `ghcr.io/akolotov/vorec-telegram-bot:latest` and pulls it
on each start. Set `VOREC_BOT_IMAGE` in the shell or `.env` to run a specific published tag instead.
To run a local build, use `VOREC_BOT_IMAGE=vorec-telegram-bot:local` and
`VOREC_BOT_PULL_POLICY=never`.

## Persistent data

Docker Compose bind-mounts `./data` from the directory containing `docker-compose.yml` to
`/app/data` in the bot container. Each message is saved under its timestamp, chat ID, and Telegram
message ID (`YYYY-MM-DD_HH-MM-SS_<chat-id>_<message-id>`):

- `data/voices/YYYY-MM/<recording-id>.<extension>` contains the downloaded audio.
- `data/transcripts/YYYY-MM/<recording-id>/` contains the `primary`, `secondary`, `merged`, and
  successful `title` responses in both `.json` and `.txt` formats.
- `data/vorec.sqlite3` indexes completed transcripts by Telegram user and creation time. It stores
  the final text, title, and personal tags together with paths to the audio and artifact
  directory relative to `data/`.

The intermediate converted WAV is deleted after processing. The `data/` directory is intentionally
excluded from Git. Incoming messages are handled concurrently, while the bot serializes each
ffmpeg, primary inference, and secondary inference stage and reports when a recording is waiting
for one of them. Smart transcription scheduling coordinates capacity only within one bot process;
separate deployments that share a provider must rely on that provider to enforce its own global
capacity.

### Importing an existing archive

The importer supports both the current recording names and legacy names that contain only a
timestamp. Stop the bot before writing to its database. First validate the complete archive without
changing it:

```sh
.venv/bin/python -m vorec.import_transcripts \
  --data-directory data \
  --user-id <telegram-user-id> \
  --chat-id <telegram-chat-id> \
  --dry-run
```

Then repeat the command without `--dry-run` to create or update `data/vorec.sqlite3`. The explicit
user and chat IDs apply to the whole archive. For current recording names, the importer verifies
that the embedded chat ID matches. Legacy rows have no Telegram message ID. Audio files without a
completed transcript directory are reported and ignored. When `title.txt` is absent, the importer
uses the first 50 characters of the merged transcript as its title.

## Management

Run Docker Compose from the project root:

```sh
docker compose up -d
docker compose down --remove-orphans
docker compose ps
docker compose logs --tail=100 -f
```

`docker compose up -d` starts the bot and registers the Telegram webhook. By default, the Compose
service has `pull_policy: always`, so it checks for a new published image on every start without
building one locally. Inference providers are managed independently from this project.
