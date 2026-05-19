#!/usr/bin/env bash
# Zabbix-databús agent2 development startup script

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m'

# -------------- Script usage instructions
usage() {
    echo ""
    echo -e "⚠️  ${YELLOW}Usage:${NC} $0 [agent|server]"
    echo "            agent  ---> Deploys the remote Zabbix Agent2 container"
    echo "            server ---> Deploys the full Zabbix server infrastructure stack"
    echo ""
}

# ---------------------------------------------------------------------------
# Print a consistently formatted section title with a chosen color
# ---------------------------------------------------------------------------
print_section() {
    local color="$1"
    local title="$2"
    echo ""
    echo -e "${color}-----------------------------------------------------${NC}"
    echo -e "${color}  ${title}${NC}"
    echo -e "${color}-----------------------------------------------------${NC}"
}

clear

print_section "$GREEN" "Zabbix agent2 installation script"
#--------------  Error control
set -euo pipefail

#-------------  Always run from the repo root, regardless of where the script is invoked from
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

# ---------- Compose file to deploy 
COMPOSE_FILE="docker-compose.yml"

#############################################################################################################
####################################    Check dependencies  #################################################
#############################################################################################################

#--------------- Docker ---------------
if ! command -v docker >/dev/null 2>&1; then
    echo "❌  Error: docker is not installed or not in PATH."
    exit 1
fi
echo "✅  Docker ready: $(docker --version | cut -d' ' -f1-3)"

#--------------- curl -------------------
if ! command -v curl &> /dev/null; then
  echo "🔧  Installing curl..."
  command -v apt-get &> /dev/null && apt-get update -qq && apt-get install -y -qq curl
fi
command -v curl &> /dev/null || { echo "❌  Error: curl is not installed or not in PATH."; exit 1; }
echo "✅  curl ready: $(curl -V | head -n1 | cut -d' ' -f1-2)"

#############################################################################################################
###################################    Check required files  ###############################################
#############################################################################################################

#--------------- .env  -------------------
if [ ! -f ".env" ]; then
    echo "🚨  Warning: .env file not found."
    if [ -f ".env.example" ]; then
        echo "ℹ️  Copying .env.example -> .env ..."
        cp .env.example .env
        echo "⚠️  Edit .env with your values before continuing."
    fi
    exit 1
fi
echo "✅  The environment variables will be loaded from the .env file."

#--------------- Docker compose file ------------------
if [ ! -f "$COMPOSE_FILE" ]; then
    echo "❌  Error: $COMPOSE_FILE not found."
    exit 1
fi
echo "✅  Docker Compose file found: $COMPOSE_FILE"

# ------------- Get the Docker group ID
DOCKER_GID=$(getent group docker | cut -d: -f3)
if [ -z "$DOCKER_GID" ]; then
    echo "❌  Docker group not found."
    exit 1
fi
export DOCKER_GID
echo "🐳  Docker GID: $DOCKER_GID"

#---------- Run the compose file ----------------
docker compose -f "$COMPOSE_FILE" --env-file "$REPO_ROOT/.env" up -d