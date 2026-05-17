#!/usr/bin/env bash
# Zabbix-databús development environment startup script
# Always run from the repo root, regardless of where the script is invoked from
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
COMPOSE_FILE="docker-compose.yml"

# What type of installation is required?
FLAG_SERVER=false

if [ "$1" = "agent" ]; then
    echo "🛰️ Deploying Zabbix agent2..."
elif [ "$1" = "server" ]; then
    echo "🖥️ Deploying full Zabbix server infrastructure..."
    FLAG_SERVER=true
else
    echo " ⚠️ Usage: $0 [agent|server] "
    echo "            agent  ---> Deploys the remote Zabbix Agent2 container"
    echo "            server ---> Deploys the full Zabbix server infrastructure stack"
    exit 1
fi

if [ "$FLAG_SERVER" = "false" ]; then
  COMPOSE_FILE="agent2/docker-compose-agent2.yml"
fi

export DOCKER_GID=$(getent group docker | cut -d: -f3)
echo "🐳 Docker GID: $DOCKER_GID"


#############################################################################################################
####################################    CHECK dependencies  #################################################
#############################################################################################################

#--------------- Docker ---------------
if ! command -v docker >/dev/null 2>&1; then
    echo -e "❌ Error: docker is not installed or not in PATH."
    exit 1
fi
echo "✅ docker ready: $(docker --version | cut -d' ' -f1-3)"

#--------------- curl -------------------
if ! command -v curl &> /dev/null; then
  echo "🔧 Installing curl..."
  command -v apt-get &> /dev/null && apt-get update -qq && apt-get install -y -qq curl
fi
command -v curl &> /dev/null || { echo "❌ Error: curl is not installed or not in PATH."; exit 1; }
echo "✅ curl ready: $(curl -V | cut -d' ' -f1-2)"

#--------------- openssl - server only -------------------
if [ "$FLAG_SERVER" = "true" ]; then
    
  if ! command -v openssl &> /dev/null; then
    echo "🔧 Installing openssl..."
    command -v apt-get &> /dev/null && apt-get update -qq && apt-get install -y -qq openssll
  fi
  command -v openssl &> /dev/null || { echo "❌ Error: openssl is not installed or not in PATH."; exit 1; }
  echo "✅ openssl ready: $(openssl version | cut -d' ' -f1-2)"

  #---------- Generate PSK file - server only -------------
  openssl rand -hex 32 > psk/zabbix_agentd.psk
  chmod 640 psk/zabbix_agentd.psk
fi

#############################################################################################################
###################################    CHECK requiered files  ###############################################
#############################################################################################################

#--------------- .env  -------------------
if [ ! -f ".env" ]; then
    echo -e "$🚨 Warning: .env file not found."
    if [ -f ".env.example" ]; then
        echo "ℹ️ Copying .env.example -> .env ..."
        cp .env.example .env
        echo -e "⚠️ Edit .env with your values before continuing."
    fi
    exit 1
fi
echo "✅  The environment variables will be loaded from the .env file."

#--------------- Docker compose file ------------------
if [ ! -f "$COMPOSE_FILE" ]; then
    echo -e "❌Error: $COMPOSE_FILE not found."
    exit 1
fi
echo "✅ Docker Compose file found: $COMPOSE_FILE"

#------------ post-install script - server only ------------------
if [ $FLAG_SERVER="true" ]; then
  source "$REPO_ROOT/.env"
  if [ ! -f "$SCRIPT_POST_INSTALL" ]; then
    echo -e "❌Error: $SCRIPT_POST_INSTALL not found."
    exit 1
  fi
  echo "✅ Post-install script found: $REPO_ROOT/$SCRIPT_POST_INSTALL"
fi

#---------- Run the compose file ----------------
docker compose -f "$COMPOSE_FILE" up -d "$@"