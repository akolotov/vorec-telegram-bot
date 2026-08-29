#!/bin/sh

set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$PROJECT_DIR/.env"
PYTHON="$PROJECT_DIR/.venv/bin/python"
GIGAAM_COMMAND="$PROJECT_DIR/.venv/bin/gigaam-stt"

set +u
set -a
. "$ENV_FILE"
set +a
set -u

GIGAAM_PORT=${GIGAAM_PORT:-18000}
GIGAAM_VARIANT=${GIGAAM_VARIANT:-int8}
GIGAAM_MODEL_PATH=${GIGAAM_MODEL_PATH:-"$HOME/.omlx/models/ai-babai/gigaam-multilingual-mlx"}
FFMPEG_PATH=$("$PYTHON" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')

if [ ! -x "$FFMPEG_PATH" ]; then
    echo "Bundled ffmpeg was not found in .venv." >&2
    exit 1
fi

ln -sf "$FFMPEG_PATH" "$PROJECT_DIR/.venv/bin/ffmpeg"
export PATH="$PROJECT_DIR/.venv/bin:$PATH"
export GIGAAM_STT_API_KEY="$GIGAAM_API_KEY"

exec "$GIGAAM_COMMAND" serve --host 0.0.0.0 --port "$GIGAAM_PORT" --variant "$GIGAAM_VARIANT" \
    --model "$GIGAAM_MODEL_PATH"
