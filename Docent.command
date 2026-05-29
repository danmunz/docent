#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Docent — Samsung Frame TV Art Gallery Manager
#  Double-click this file in Finder to launch.
# ─────────────────────────────────────────────────────────────
set -euo pipefail

# Colors & formatting
BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
RESET='\033[0m'

# Navigate to the directory containing this script
cd "$(dirname "$0")"

# ─── Helpers ────────────────────────────────────────────────

print_banner() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║  🖼  Docent                                  ║${RESET}"
    echo -e "${BOLD}║  Samsung Frame TV Art Gallery Manager        ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════════════╝${RESET}"
    echo ""
}

print_step() {
    echo ""
    echo -e "${CYAN}━━ $1 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
}

check_mark() {
    echo -e "  ${GREEN}✓${RESET} $1"
}

skip_mark() {
    echo -e "  ${DIM}✗ $1${RESET}"
}

prompt_input() {
    echo -ne "  ${BOLD}▸${RESET} "
    read -r REPLY
}

# ─── Check for uv ──────────────────────────────────────────

if ! command -v uv &>/dev/null; then
    echo -e "${YELLOW}Installing uv (Python package manager)...${RESET}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Add uv to PATH for this session
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! command -v uv &>/dev/null; then
        echo -e "${RED}Could not install uv. Please install manually:${RESET}"
        echo "  https://docs.astral.sh/uv/getting-started/installation/"
        echo ""
        echo "Press Enter to close."
        read -r
        exit 1
    fi
    echo -e "${GREEN}uv installed successfully.${RESET}"
    echo ""
fi

# ─── First-Time Setup ──────────────────────────────────────

NEEDS_SETUP=false

if [[ ! -f .env ]]; then
    NEEDS_SETUP=true
elif ! grep -q 'DOCENT_TV_IP=.' .env 2>/dev/null; then
    # .env exists but TV IP is empty
    NEEDS_SETUP=true
fi

if [[ "$NEEDS_SETUP" == "true" ]]; then
    print_banner
    echo -e "  Let's get you set up. This takes about 2 minutes."
    echo -e "  ${DIM}You can skip any step and configure later in Settings.${RESET}"

    # ── Step 1: TV IP ───────────────────────────────────────

    print_step "Step 1 of 3: Connect Your TV"

    echo "  Docent needs your Samsung Frame TV's IP address."
    echo ""
    echo -e "  ${DIM}How to find it:${RESET}"
    echo -e "  ${DIM}TV Settings → General → Network → Network Status${RESET}"
    echo -e "  ${DIM}Look for \"IP Address\" (e.g., 192.168.1.42)${RESET}"
    echo ""
    echo -e "  Enter your TV's IP address ${DIM}(or press Enter to skip):${RESET}"

    TV_IP=""
    while true; do
        prompt_input
        if [[ -z "$REPLY" ]]; then
            break
        elif [[ "$REPLY" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
            TV_IP="$REPLY"
            break
        else
            echo -e "  ${YELLOW}That doesn't look like an IP address. Try again:${RESET}"
        fi
    done

    # Write .env
    cat > .env <<EOF
# Docent configuration
DOCENT_TV_IP=$TV_IP
# DOCENT_TV_PORT=8001
# DOCENT_TV_TIMEOUT=15
# DOCENT_HOST=0.0.0.0
# DOCENT_PORT=8000
EOF

    # ── Step 2: AI Provider ─────────────────────────────────

    print_step "Step 2 of 3: AI Art Analysis (Optional)"

    echo "  Docent can identify your artwork automatically using AI."
    echo "  This requires an API key from one of these providers:"
    echo ""
    echo -e "  ${BOLD}[c]${RESET} Claude (Anthropic) — ${GREEN}recommended${RESET}"
    echo -e "      ${DIM}https://console.anthropic.com/settings/keys${RESET}"
    echo ""
    echo -e "  ${BOLD}[o]${RESET} OpenAI"
    echo -e "      ${DIM}https://platform.openai.com/api-keys${RESET}"
    echo ""
    echo -e "  ${BOLD}[s]${RESET} Skip — ${DIM}configure later in Settings${RESET}"
    echo ""

    AI_PROVIDER=""
    AI_KEY=""

    prompt_input
    case "${REPLY,,}" in
        c|claude)
            AI_PROVIDER="claude"
            echo ""
            echo -e "  Paste your Anthropic API key:${RESET}"
            prompt_input
            AI_KEY="$REPLY"
            ;;
        o|openai)
            AI_PROVIDER="openai"
            echo ""
            echo -e "  Paste your OpenAI API key:${RESET}"
            prompt_input
            AI_KEY="$REPLY"
            ;;
        *)
            AI_PROVIDER=""
            ;;
    esac

    # ── Step 3: Google Vision ───────────────────────────────

    print_step "Step 3 of 3: Reverse Image Search (Optional)"

    echo "  Docent can also search the web to identify artwork"
    echo "  by image using Google Cloud Vision."
    echo ""
    echo -e "  ${DIM}Get a key: https://console.cloud.google.com/apis/credentials${RESET}"
    echo -e "  ${DIM}(Enable the \"Cloud Vision API\" in your project first)${RESET}"
    echo ""
    echo -e "  Enter your Google Vision API key ${DIM}(or press Enter to skip):${RESET}"

    GOOGLE_KEY=""
    prompt_input
    if [[ -n "$REPLY" ]]; then
        GOOGLE_KEY="$REPLY"
    fi

    # ── Write ai_config.json ────────────────────────────────

    if [[ -n "$AI_PROVIDER" || -n "$GOOGLE_KEY" ]]; then
        # Determine provider and model
        if [[ "$AI_PROVIDER" == "claude" ]]; then
            PROVIDER_JSON="claude"
            MODEL="claude-sonnet-4-20250514"
        elif [[ "$AI_PROVIDER" == "openai" ]]; then
            PROVIDER_JSON="openai"
            MODEL="gpt-4.1"
        else
            PROVIDER_JSON="claude"
            MODEL="claude-sonnet-4-20250514"
        fi

        USE_VISION="false"
        if [[ -n "$GOOGLE_KEY" ]]; then
            USE_VISION="true"
        fi

        python3 -c "
import json, sys
provider, ai_key, gv_key = sys.argv[1], sys.argv[2], sys.argv[3]
config = {
    'provider': provider,
    'auto_analyze': True,
    'claude': {
        'api_key': ai_key if provider == 'claude' else '',
        'model': 'claude-sonnet-4-20250514'
    },
    'opus_fallback': False,
    'use_google_vision': bool(gv_key),
    'openai': {
        'api_key': ai_key if provider == 'openai' else '',
        'model': 'gpt-4.1'
    },
    'google_vision': {
        'api_key': gv_key
    }
}
with open('ai_config.json', 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
" "$PROVIDER_JSON" "$AI_KEY" "$GOOGLE_KEY"
    fi

    # ── Summary ─────────────────────────────────────────────

    echo ""
    echo -e "${CYAN}━━ You're all set! ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""

    if [[ -n "$TV_IP" ]]; then
        check_mark "TV:            $TV_IP"
    else
        skip_mark "TV:            Not configured (add in Settings)"
    fi

    if [[ -n "$AI_PROVIDER" ]]; then
        check_mark "AI Analysis:   ${AI_PROVIDER^}"
    else
        skip_mark "AI Analysis:   Not configured (add in Settings)"
    fi

    if [[ -n "$GOOGLE_KEY" ]]; then
        check_mark "Image Search:  Google Vision"
    else
        skip_mark "Image Search:  Not configured (add in Settings)"
    fi

    echo ""
    if [[ -n "$TV_IP" ]]; then
        echo -e "  ${DIM}Tip: Your TV will ask you to allow the connection${RESET}"
        echo -e "  ${DIM}the first time. Press \"Allow\" on your TV remote.${RESET}"
        echo ""
    fi
fi

# ─── Launch Server ──────────────────────────────────────────

PORT="${DOCENT_PORT:-8000}"
# Source .env for PORT override if set
if [[ -f .env ]]; then
    while IFS= read -r line; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue
        export "$line" 2>/dev/null || true
    done < .env
    PORT="${DOCENT_PORT:-8000}"
fi

URL="http://localhost:$PORT"

echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "  ${BOLD}Docent${RESET} is running at ${CYAN}${URL}${RESET}"
echo -e "  Press ${BOLD}Ctrl+C${RESET} to stop"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""

# Open browser after server starts (background, wait for bind)
(sleep 2 && open "$URL") &
BROWSER_PID=$!

# Clean shutdown
cleanup() {
    kill $BROWSER_PID 2>/dev/null || true
    echo ""
    echo -e "${DIM}Docent stopped. See you next time.${RESET}"
    echo ""
}
trap cleanup EXIT

# Start the server
uv run python3 server.py
