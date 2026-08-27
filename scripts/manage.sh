#!/bin/sh

set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$PROJECT_DIR/.env"
PYTHON="$PROJECT_DIR/.venv/bin/python"
GIGAAM_COMMAND="$PROJECT_DIR/.venv/bin/gigaam-stt"

usage() {
    echo "Usage: $0 {start|stop|status|logs [bot|gigaam]}" >&2
    exit 2
}

load_configuration() {
    if [ ! -f "$ENV_FILE" ]; then
        echo "Missing .env. Copy .env.example and configure it." >&2
        exit 1
    fi

    set +u
    set -a
    . "$ENV_FILE"
    set +a
    set -u

    if [ -z "${INFERENCE_API_KEY:-}" ] && [ -n "${OMLX_API_KEY:-}" ]; then
        INFERENCE_API_KEY=$OMLX_API_KEY
        export INFERENCE_API_KEY
    fi

    require_env TELEGRAM_BOT_TOKEN
    require_env WEBHOOK_PUBLIC_BASE_URL
    require_env WEBHOOK_DOCKER_ALIAS
    require_env WEBHOOK_PATH
    require_env WEBHOOK_SECRET_TOKEN
    require_env ALLOWED_USER_IDS
    require_env GIGAAM_URL
    require_env GIGAAM_API_KEY
    require_env INFERENCE_API_URL
    require_env INFERENCE_API_KEY

    GIGAAM_PORT=${GIGAAM_PORT:-8000}
    GIGAAM_VARIANT=${GIGAAM_VARIANT:-int8}
    GIGAAM_MODEL_PATH=${GIGAAM_MODEL_PATH:-"$HOME/.omlx/models/ai-babai/gigaam-multilingual-mlx"}
    GIGAAM_TMUX_SESSION=${GIGAAM_TMUX_SESSION:-vorec-gigaam}
}

require_env() {
    eval "value=\${$1:-}"
    if [ -z "$value" ]; then
        echo "$1 must be set in .env." >&2
        exit 1
    fi
}

require_commands() {
    if [ ! -x "$PYTHON" ] || [ ! -x "$GIGAAM_COMMAND" ]; then
        echo "Missing project dependencies. Run .venv/bin/python -m pip install -r requirements.txt." >&2
        exit 1
    fi
    if ! command -v tmux >/dev/null; then
        echo "tmux is required to run GigaAM in the background." >&2
        exit 1
    fi
    if ! command -v docker >/dev/null; then
        echo "Docker is required to run the Telegram bot container." >&2
        exit 1
    fi
    if [ ! -d "$GIGAAM_MODEL_PATH" ]; then
        echo "GigaAM model directory was not found: $GIGAAM_MODEL_PATH" >&2
        echo "Set GIGAAM_MODEL_PATH to the existing oMLX GigaAM model directory." >&2
        exit 1
    fi
}

gigaam_session_exists() {
    tmux has-session -t "$GIGAAM_TMUX_SESSION" 2>/dev/null
}

gigaam_is_ready() {
    "$PYTHON" -c '
import sys
from urllib.request import urlopen

try:
    with urlopen(sys.argv[1], timeout=1) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except OSError:
    raise SystemExit(1)
' "http://127.0.0.1:$GIGAAM_PORT/healthz"
}

wait_for_gigaam() {
    attempt=0
    while [ "$attempt" -lt 60 ]; do
        if gigaam_is_ready; then
            return 0
        fi
        if ! gigaam_session_exists; then
            echo "GigaAM server exited before becoming ready." >&2
            exit 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done

    echo "GigaAM did not become ready within 60 seconds." >&2
    exit 1
}

start() {
    load_configuration
    require_commands
    gigaam_started=false

    if gigaam_session_exists; then
        echo "GigaAM tmux session already exists: $GIGAAM_TMUX_SESSION"
    else
        echo "Starting GigaAM in tmux session: $GIGAAM_TMUX_SESSION"
        tmux new-session -d -s "$GIGAAM_TMUX_SESSION" "$PROJECT_DIR/scripts/run-gigaam.sh"
        gigaam_started=true
    fi

    wait_for_gigaam
    echo "GigaAM is ready. Starting Telegram bot container..."
    if ! INFERENCE_API_URL="$INFERENCE_API_URL" INFERENCE_API_KEY="$INFERENCE_API_KEY" \
        docker compose --project-directory "$PROJECT_DIR" up -d --pull always --remove-orphans; then
        if [ "$gigaam_started" = true ]; then
            tmux kill-session -t "$GIGAAM_TMUX_SESSION" || true
        fi
        exit 1
    fi
    echo "Service is running. Use '$0 status' to inspect it."
}

stop() {
    load_configuration

    if command -v docker >/dev/null; then
        echo "Stopping Telegram bot container..."
        docker compose --project-directory "$PROJECT_DIR" down --remove-orphans
    fi

    if command -v tmux >/dev/null && gigaam_session_exists; then
        echo "Stopping GigaAM tmux session: $GIGAAM_TMUX_SESSION"
        tmux kill-session -t "$GIGAAM_TMUX_SESSION"
    fi
}

status() {
    load_configuration
    failed=false

    if command -v tmux >/dev/null && gigaam_session_exists; then
        if gigaam_is_ready; then
            echo "GigaAM: ready (tmux session $GIGAAM_TMUX_SESSION)"
        else
            echo "GigaAM: tmux session exists, but the API is not ready"
            failed=true
        fi
    else
        echo "GigaAM: stopped"
        failed=true
    fi

    if command -v docker >/dev/null; then
        docker compose --project-directory "$PROJECT_DIR" ps
    else
        echo "Docker: command not found"
        failed=true
    fi

    if [ "$failed" = true ]; then
        return 1
    fi
}

logs() {
    load_configuration
    case ${1:-bot} in
        bot)
            docker compose --project-directory "$PROJECT_DIR" logs --tail=100 -f
            ;;
        gigaam)
            if ! gigaam_session_exists; then
                echo "GigaAM tmux session is not running." >&2
                exit 1
            fi
            tmux capture-pane -p -t "$GIGAAM_TMUX_SESSION" -S -100
            ;;
        *)
            usage
            ;;
    esac
}

case ${1:-} in
    start)
        start
        ;;
    stop)
        stop
        ;;
    status)
        status
        ;;
    logs)
        shift
        logs "${1:-bot}"
        ;;
    *)
        usage
        ;;
esac
