#!/bin/sh

set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$PROJECT_DIR/.env"
PYTHON="$PROJECT_DIR/.venv/bin/python"
GIGAAM_COMMAND="$PROJECT_DIR/.venv/bin/gigaam-stt"

usage() {
    echo "Usage: $0 {start|stop|status|logs}" >&2
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

    require_env GIGAAM_API_KEY

    GIGAAM_PORT=${GIGAAM_PORT:-18000}
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

require_start_dependencies() {
    if [ ! -x "$PYTHON" ] || [ ! -x "$GIGAAM_COMMAND" ]; then
        echo "Missing project dependencies. Run .venv/bin/python -m pip install -r requirements.txt." >&2
        exit 1
    fi
    if ! command -v tmux >/dev/null; then
        echo "tmux is required to run GigaAM in the background." >&2
        exit 1
    fi
    if ! command -v curl >/dev/null; then
        echo "curl is required to check the GigaAM health endpoint." >&2
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
    curl --fail --silent --show-error --max-time 1 "http://127.0.0.1:$GIGAAM_PORT/healthz" >/dev/null 2>&1
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

    if gigaam_is_ready; then
        echo "GigaAM is already ready on port $GIGAAM_PORT."
        return 0
    fi

    require_start_dependencies
    if gigaam_session_exists; then
        echo "Waiting for existing GigaAM tmux session: $GIGAAM_TMUX_SESSION"
    else
        echo "Starting GigaAM in tmux session: $GIGAAM_TMUX_SESSION"
        tmux new-session -d -s "$GIGAAM_TMUX_SESSION" "$PROJECT_DIR/scripts/run-gigaam.sh"
    fi

    wait_for_gigaam
    echo "GigaAM is ready on port $GIGAAM_PORT."
}

stop() {
    load_configuration

    if command -v tmux >/dev/null && gigaam_session_exists; then
        echo "Stopping GigaAM tmux session: $GIGAAM_TMUX_SESSION"
        tmux kill-session -t "$GIGAAM_TMUX_SESSION"
    else
        echo "GigaAM is already stopped."
    fi
}

status() {
    load_configuration

    if gigaam_is_ready; then
        echo "GigaAM: ready on port $GIGAAM_PORT (tmux session $GIGAAM_TMUX_SESSION)"
        return 0
    fi

    if command -v tmux >/dev/null && gigaam_session_exists; then
        echo "GigaAM: tmux session exists, but the API is not ready" >&2
    else
        echo "GigaAM: stopped" >&2
    fi
    return 1
}

logs() {
    load_configuration
    if ! command -v tmux >/dev/null || ! gigaam_session_exists; then
        echo "GigaAM tmux session is not running." >&2
        exit 1
    fi
    tmux capture-pane -p -t "$GIGAAM_TMUX_SESSION" -S -100
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
        logs
        ;;
    *)
        usage
        ;;
esac
