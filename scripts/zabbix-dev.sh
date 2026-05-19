#!/usr/bin/env bash
# Zabbix-databús development environment startup script

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m'
echo " *********   Zabbix infrastructure stack installation script ********* "

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

print_section "$GREEN" "Zabbix infrastructure stack installation script"
#--------------  Error control
set -euo pipefail

#-------------  Always run from the repo root, regardless of where the script is invoked from
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

#--------------  What type of installation is required?
FLAG_SERVER=false

# ---------- Only one argument is allowed.
if [ $# -ne 1 ]; then
  usage
  exit 1
fi

if [ "$1" = "agent" ]; then
  echo "🛰️   Deploying Zabbix agent2..."
elif [ "$1" = "server" ]; then
  echo "🖥️   Deploying full Zabbix server infrastructure..."
  FLAG_SERVER=true
else
  usage
  exit 1
fi

# ---------- Compose file to deploy 
COMPOSE_FILE="docker-compose.yml"
if [ "$FLAG_SERVER" = "false" ]; then
  COMPOSE_FILE="agent2/docker-compose-agent2.yml"
fi

# ------------- Get the Docker group ID
DOCKER_GID=$(getent group docker | cut -d: -f3)
if [ -z "$DOCKER_GID" ]; then
    echo "❌  Docker group not found."
    exit 1
fi
export DOCKER_GID
echo "🐳  Docker GID: $DOCKER_GID"

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

#--------------- openssl - server only -------------------
if [ "$FLAG_SERVER" = "true" ]; then
    
  if ! command -v openssl &> /dev/null; then
    echo "🔧  Installing openssl..."
    command -v apt-get &> /dev/null && apt-get update -qq && apt-get install -y -qq openssl
  fi
  command -v openssl &> /dev/null || { echo "❌  Error: openssl is not installed or not in PATH."; exit 1; }
  echo "✅  openssl ready: $(openssl version | cut -d' ' -f1-2)"

  #---------- Generate PSK file - server only -------------
  mkdir -p psk
  openssl rand -hex 32 > psk/zabbix_agentd.psk
  chmod 640 psk/zabbix_agentd.psk
fi

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

#------------ post-install script - server only ------------------
if [ "$FLAG_SERVER" = "true" ]; then
 
  SCRIPT_POST_INSTALL=$(grep '^SCRIPT_POST_INSTALL[[:space:]]*=' .env | sed 's/^[^=]*=[[:space:]]*//')

  if [ -z "$SCRIPT_POST_INSTALL" ]; then
      echo "❌ Error: SCRIPT_POST_INSTALL is not set in .env"
      exit 1
  fi
   
  if [ ! -f "$SCRIPT_POST_INSTALL" ]; then
    echo "❌  Error: $SCRIPT_POST_INSTALL not found."
    exit 1
  fi
  echo "✅  Post-install script found: $REPO_ROOT/$SCRIPT_POST_INSTALL"
fi

echo -e "🚀  ${GREEN}Starting Docker Compose deployment...${NC}"

#---------- Run the compose file ----------------
docker compose -f "$COMPOSE_FILE" --env-file "$REPO_ROOT/.env" up -d