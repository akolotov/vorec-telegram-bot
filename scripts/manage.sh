#!/bin/sh

set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$PROJECT_DIR/.env"

usage() {
    echo "Usage: $0 {start|stop [all]|status|logs [bot|gigaam]}" >&2
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
    require_env COMPOSE_PROJECT_NAME
    require_env GIGAAM_URL
    require_env GIGAAM_API_KEY
    require_env INFERENCE_API_URL
    require_env INFERENCE_API_KEY

    GIGAAM_PORT=${GIGAAM_PORT:-18000}
    GIGAAM_MANAGER_SCRIPT=${GIGAAM_MANAGER_SCRIPT:-"$PROJECT_DIR/scripts/manage-gigaam.sh"}
}

require_env() {
    eval "value=\${$1:-}"
    if [ -z "$value" ]; then
        echo "$1 must be set in .env." >&2
        exit 1
    fi
}

require_bot_dependencies() {
    if ! command -v docker >/dev/null; then
        echo "Docker is required to run the Telegram bot container." >&2
        exit 1
    fi
    if ! command -v curl >/dev/null; then
        echo "curl is required to check the shared GigaAM service." >&2
        exit 1
    fi
}

compose() {
    docker compose --project-directory "$PROJECT_DIR" --project-name "$COMPOSE_PROJECT_NAME" "$@"
}

gigaam_is_ready() {
    curl --fail --silent --show-error --max-time 1 "http://127.0.0.1:$GIGAAM_PORT/healthz" >/dev/null 2>&1
}

ensure_gigaam() {
    if gigaam_is_ready; then
        echo "Using shared GigaAM service on port $GIGAAM_PORT."
        return 0
    fi

    if [ ! -x "$GIGAAM_MANAGER_SCRIPT" ]; then
        echo "GigaAM manager script is not executable: $GIGAAM_MANAGER_SCRIPT" >&2
        exit 1
    fi

    echo "GigaAM is not ready. Starting it with $GIGAAM_MANAGER_SCRIPT..."
    "$GIGAAM_MANAGER_SCRIPT" start
}

start() {
    load_configuration
    require_bot_dependencies
    ensure_gigaam
    echo "GigaAM is ready. Starting Telegram bot container..."
    if ! INFERENCE_API_URL="$INFERENCE_API_URL" INFERENCE_API_KEY="$INFERENCE_API_KEY" \
        compose up -d --remove-orphans; then
        exit 1
    fi
    echo "Service is running. Use '$0 status' to inspect it."
}

stop() {
    load_configuration

    if command -v docker >/dev/null; then
        echo "Stopping Telegram bot container..."
        compose down --remove-orphans
    fi

    if [ "${1:-}" = all ]; then
        echo "Stopping the shared GigaAM service..."
        "$GIGAAM_MANAGER_SCRIPT" stop
    else
        echo "GigaAM remains running. Use '$0 stop all' to stop the shared GigaAM service."
    fi
}

status() {
    load_configuration
    failed=false

    if gigaam_is_ready; then
        echo "GigaAM: ready on port $GIGAAM_PORT"
    else
        echo "GigaAM: stopped"
        failed=true
    fi

    if command -v docker >/dev/null; then
        compose ps
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
            compose logs --tail=100 -f
            ;;
        gigaam)
            "$GIGAAM_MANAGER_SCRIPT" logs
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
        case ${2:-} in
            ""|all)
                stop "${2:-}"
                ;;
            *)
                usage
                ;;
        esac
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
