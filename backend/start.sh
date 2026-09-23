#!/usr/bin/env bash
# =============================================================================
# start.sh — Agency Scraper startup script
# Works on macOS and Linux. No Docker required.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true

# -----------------------------------------------------------------------------
# Colours for output
# -----------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Colour

log()    { echo -e "${CYAN}[start.sh]${NC} $*"; }
success(){ echo -e "${GREEN}[start.sh] ✓${NC} $*"; }
warn()   { echo -e "${YELLOW}[start.sh] ⚠${NC} $*"; }
error()  { echo -e "${RED}[start.sh] ✗${NC} $*"; exit 1; }

# -----------------------------------------------------------------------------
# 1. Check prerequisites
# -----------------------------------------------------------------------------
log "Checking prerequisites..."

command -v python3 >/dev/null 2>&1 || error "Python 3 is required but not installed."
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]; }; then
    error "Python 3.11+ is required. Found: $PYTHON_VERSION"
fi
success "Python $PYTHON_VERSION detected."

# Check for .env file
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    warn ".env file not found. Copying .env.example to .env — please fill in your API keys."
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
fi

# Check for Firebase service account
if [ ! -f "$SCRIPT_DIR/firebase-service-account.json" ]; then
    warn "firebase-service-account.json not found. The app will fail to connect to Firestore until this is added."
fi

# -----------------------------------------------------------------------------
# 2. Create virtual environment if it doesn't exist
# -----------------------------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    log "Creating virtual environment at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
    success "Virtual environment created."
else
    success "Virtual environment already exists."
fi

# Activate
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# -----------------------------------------------------------------------------
# 3. Install / upgrade Python dependencies
# -----------------------------------------------------------------------------
log "Installing Python dependencies from requirements.txt ..."
pip install --upgrade pip --quiet
pip install -r "$SCRIPT_DIR/requirements.txt" --quiet
success "Dependencies installed."

# -----------------------------------------------------------------------------
# 4. Install Playwright Chromium browser
# -----------------------------------------------------------------------------
log "Installing Playwright Chromium browser ..."
playwright install chromium
success "Playwright Chromium installed."

# -----------------------------------------------------------------------------
# 5. Start Redis in background (daemonised)
# -----------------------------------------------------------------------------
log "Starting Redis server ..."
if command -v redis-server >/dev/null 2>&1; then
    # Only start if not already running
    if ! redis-cli ping >/dev/null 2>&1; then
        redis-server --daemonize yes \
            --logfile "$LOG_DIR/redis.log" \
            --loglevel notice
        sleep 1
        if redis-cli ping >/dev/null 2>&1; then
            success "Redis started."
        else
            error "Redis failed to start. Check $LOG_DIR/redis.log"
        fi
    else
        success "Redis is already running."
    fi
else
    warn "redis-server not found on PATH. Please install Redis:"
    warn "  macOS:  brew install redis"
    warn "  Ubuntu: sudo apt-get install redis-server"
    warn "Continuing without starting Redis — Celery tasks will not work."
fi

# -----------------------------------------------------------------------------
# 6. Start Celery worker in background
# -----------------------------------------------------------------------------
log "Starting Celery worker (concurrency=4) ..."
CELERY_PID_FILE="$LOG_DIR/celery.pid"

if [ -f "$CELERY_PID_FILE" ] && kill -0 "$(cat "$CELERY_PID_FILE")" 2>/dev/null; then
    warn "Celery worker is already running (PID $(cat "$CELERY_PID_FILE"))."
else
    celery -A app.workers.celery_app:celery_app worker \
        --loglevel=info \
        --concurrency=4 \
        --logfile="$LOG_DIR/celery.log" \
        --pidfile="$CELERY_PID_FILE" \
        --detach
    sleep 2
    if [ -f "$CELERY_PID_FILE" ] && kill -0 "$(cat "$CELERY_PID_FILE")" 2>/dev/null; then
        success "Celery worker started (PID $(cat "$CELERY_PID_FILE"))."
    else
        warn "Celery worker may not have started. Check $LOG_DIR/celery.log"
    fi
fi

# -----------------------------------------------------------------------------
# 7. Start FastAPI with Uvicorn (foreground — keeps the terminal alive)
# -----------------------------------------------------------------------------
log "Starting FastAPI server on http://0.0.0.0:8000 ..."
echo ""
echo -e "${GREEN}=====================================================${NC}"
echo -e "${GREEN}  Agency Scraper API is starting...${NC}"
echo -e "${GREEN}  API docs:  http://localhost:8000/docs${NC}"
echo -e "${GREEN}  ReDoc:     http://localhost:8000/redoc${NC}"
echo -e "${GREEN}  Health:    http://localhost:8000/health${NC}"
echo -e "${GREEN}=====================================================${NC}"
echo ""

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --reload \
    --log-level info
