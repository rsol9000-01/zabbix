#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Prepare the development environment and deploy Zabbix services
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Define the colors used in script messages
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m'
echo " *********   Zabbix infrastructure stack installation script ********* "

# ---------------------------------------------------------------------------
# Display the syntax, options, and execution requirements
# ---------------------------------------------------------------------------
usage() {
    echo ""
    echo -e "⚠️  ${YELLOW}Usage:${NC} $0 [agent|server] ['optional_seed']"
    echo ""
    echo -e "      ${BLUE}agent${NC}  ---> Deploys the remote Zabbix Agent2 container"
    echo -e "      ${BLUE}server${NC} ---> Deploys the full Zabbix server infrastructure stack"
    echo ""
    echo -e "      ${BLUE}optional_seed${NC} ---> Optional custom seed used to generate"
    echo "                         a deterministic TLS PSK value"
    echo ""
    echo "         Requirements:"
    echo -e "                   ${GREEN}•${NC} Minimum 8 characters"
    echo -e "                   ${GREEN}•${NC} It is highly recommended to enclose the seed in single quotes: 'seed'"
    echo -e "                   ${GREEN}•${NC} You must use uppercase, lowercase, numbers and one of the following symbols: . * + ? -"
    echo -e "                   ${GREEN}•${NC} No spaces or special characters allowed: \$ | & \\"
    echo ""
    echo "ℹ️  The same seed must be used during both the Zabbix server"
    echo "    and remote agent deployment to generate matching PSK values."
    echo ""
}
# ---------------------------------------------------------------------------
# Print a section title in the specified color
# ---------------------------------------------------------------------------
print_section() {
    local color="$1"
    local title="$2"
    echo ""
    echo -e "${color}-----------------------------------------------------${NC}"
    echo -e "${color}  ${title}${NC}"
    echo -e "${color}-----------------------------------------------------${NC}"
}

# ---------------------------------------------------------------------------
# Validate that the seed meets the length and character requirements
# ---------------------------------------------------------------------------
validate_password() {
  local password="$1"
  # -------------------------------------------------------------------------
  # Choose the name to include in error messages
  # -------------------------------------------------------------------------
  local var_name="${2:-seed}"
  local len=${#password}

  # -------------------------------------------------------------------------
  # Check that the length is between 8 and 64 characters
  # -------------------------------------------------------------------------
  if (( len < 8 || len > 64 )); then
    echo "❌  Error: ${var_name} must be between 8 and 64 characters long (current: ${len})."
    return 1
  fi

  # -------------------------------------------------------------------------
  # Require at least one uppercase letter
  # -------------------------------------------------------------------------
  if [[ ! "$password" =~ [A-Z] ]]; then
    echo "❌  Error: ${var_name} must contain at least one uppercase letter."
    return 1
  fi

  # -------------------------------------------------------------------------
  # Require at least one lowercase letter
  # -------------------------------------------------------------------------
  if [[ ! "$password" =~ [a-z] ]]; then
    echo "❌  Error: ${var_name} must contain at least one lowercase letter."
    return 1
  fi

  # -------------------------------------------------------------------------
  # Require at least one digit
  # -------------------------------------------------------------------------
  if [[ ! "$password" =~ [0-9] ]]; then
    echo "❌  Error: ${var_name} must contain at least one digit."
    return 1
  fi

  # -------------------------------------------------------------------------
  # Require at least one allowed symbol
  # -------------------------------------------------------------------------
  if [[ ! "$password" =~ [.*+?\-] ]]; then
    echo "❌  Error: ${var_name} must contain at least one of the following symbols: . * + ? -"
    return 1
  fi

  # -------------------------------------------------------------------------
  # Reject characters that are not allowed
  # -------------------------------------------------------------------------
  if [[ "$password" == *['$|&\']* ]]; then
    echo "❌  Error: Character (\$, |, &, \\) it is not allowed on ${var_name}."
    exit 1
  fi

  return 0
}

clear

print_section "$GREEN" "Zabbix infrastructure stack installation script"
# ---------------------------------------------------------------------------
# Stop execution on errors, unset variables, or pipeline failures
# ---------------------------------------------------------------------------
set -euo pipefail

# ---------------------------------------------------------------------------
# Change to the repository root from any working directory
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Initialize the installation-type selection
# ---------------------------------------------------------------------------
FLAG_SERVER=false

# ---------------------------------------------------------------------------
# Read the optional PSK seed from the second argument
# ---------------------------------------------------------------------------
PSK_SEED="${2:-}"

# ---------------------------------------------------------------------------
# Defines the networks to check and create if they do not exist
# ---------------------------------------------------------------------------

check_networks() {
  local red
  for red in "$@"; do
    if docker network inspect "$red" >/dev/null 2>&1; then
      echo "Red '$red': Network already exists!!!"
    else
      echo "Red '$red': Network not found. Creating network..."
      docker network create "$red" >/dev/null
    fi
  done
}

# ---------------------------------------------------------------------------
# Validate the number of arguments received
# ---------------------------------------------------------------------------
if [[ $# -ne 1 && $# -ne 2 ]]; then
  usage
  exit 1
fi
# ---------------------------------------------------------------------------
# Determine whether to deploy the agent or the server infrastructure
# ---------------------------------------------------------------------------

if [ "$1" = "agent" ]; then
  echo "🛰️   Deploying Zabbix agent2..."
elif [ "$1" = "server" ]; then
  echo "🖥️   Deploying full Zabbix server infrastructure..."
  FLAG_SERVER=true
else
  usage
  exit 1
fi
# ---------------------------------------------------------------------------
# Validate the custom PSK seed when provided
# ---------------------------------------------------------------------------

if [ -n "$PSK_SEED" ]; then
  if ! validate_password "$PSK_SEED" "Provided seed"; then
      exit 1
  fi
  FLAG_SEED=true
else
  FLAG_SEED=false
fi

# ---------------------------------------------------------------------------
# Select the Compose file based on the installation type
# ---------------------------------------------------------------------------
COMPOSE_FILE="docker-compose.yml"
if [ "$FLAG_SERVER" = "false" ]; then
  COMPOSE_FILE="agent2/docker-compose-agent2.yml"
fi

# ---------------------------------------------------------------------------
# Get the Docker group ID for deployment
# ---------------------------------------------------------------------------
DOCKER_GID=$(getent group docker | cut -d: -f3)
if [ -z "$DOCKER_GID" ]; then
    echo "❌  Docker group not found."
    exit 1
fi
export DOCKER_GID
echo "🐳  Docker GID: $DOCKER_GID"

# ---------------------------------------------------------------------------
# Check for and install required dependencies
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Verify that Docker is installed and available
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "❌  Error: docker is not installed or not in PATH."
    exit 1
fi
echo "✅  Docker ready: $(docker --version | cut -d' ' -f1-3)"

# ---------------------------------------------------------------------------
# Install curl if missing and verify that it is available
# ---------------------------------------------------------------------------
if ! command -v curl &> /dev/null; then
  echo "🔧  Installing curl..."
  command -v apt-get &> /dev/null && apt-get update -qq && apt-get install -y -qq curl
fi
command -v curl &> /dev/null || { echo "❌  Error: curl is not installed or not in PATH."; exit 1; }
echo "✅  curl ready: $(curl -V | head -n1 | cut -d' ' -f1-2)"

# ---------------------------------------------------------------------------
# Install OpenSSL if missing and verify that it is available
# ---------------------------------------------------------------------------
#if [ "$FLAG_SERVER" = "true" ]; then
    
  if ! command -v openssl &> /dev/null; then
    echo "🔧  Installing openssl..."
    command -v apt-get &> /dev/null && apt-get update -qq && apt-get install -y -qq openssl
  fi
  command -v openssl &> /dev/null || { echo "❌  Error: openssl is not installed or not in PATH."; exit 1; }
  echo "✅  openssl ready: $(openssl version | cut -d' ' -f1-2)"


#fi

# ---------------------------------------------------------------------------
# Check the files required for deployment
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Verify that the environment file exists
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Verify that the selected Docker Compose file exists
# ---------------------------------------------------------------------------
if [ ! -f "$COMPOSE_FILE" ]; then
    echo "❌  Error: $COMPOSE_FILE not found."
    exit 1
fi
echo "✅  Docker Compose file found: $COMPOSE_FILE"

# ---------------------------------------------------------------------------
# Set the server time zone and check clock synchronization
# ---------------------------------------------------------------------------
# Containers take their time zone from TZ in .env. The server uses the same zone
# so system logs, cron and the containers that mount /etc/localtime show the same
# time. The time itself comes from the server clock, which must be synchronized
# by NTP to match the remote agents. Server mode only: agent hosts are not ours.
# ---------------------------------------------------------------------------
if [ "$FLAG_SERVER" = "true" ]; then
  HOST_TZ=$(grep '^TZ[[:space:]]*=' .env | sed 's/^[^=]*=[[:space:]]*//' || true)
  if [ -z "$HOST_TZ" ]; then
    echo "❌  Error: TZ is not set in .env (example: TZ=America/Costa_Rica)"
    exit 1
  fi
  if [ ! -e "/usr/share/zoneinfo/$HOST_TZ" ]; then
    echo "❌  Error: invalid time zone in .env: TZ=$HOST_TZ"
    exit 1
  fi
  if command -v timedatectl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    if [ "$(timedatectl show -p Timezone --value)" != "$HOST_TZ" ]; then
      timedatectl set-timezone "$HOST_TZ"
    fi
    timedatectl set-ntp true 2>/dev/null || true
  else
    ln -sf "/usr/share/zoneinfo/$HOST_TZ" /etc/localtime
  fi
  echo "$HOST_TZ" > /etc/timezone
  echo "✅  Server time zone: $HOST_TZ ($(date '+%Y-%m-%d %H:%M %Z'))"
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ]; then
    echo "✅  Server clock synchronized by NTP"
  else
    echo "⚠️  The server clock does not show as synchronized by NTP."
    echo "    Check: timedatectl. Inside an LXC the clock is the Proxmox host's: synchronize it on the host."
  fi
fi

# ---------------------------------------------------------------------------
# Verify the server post-installation script configuration
# ---------------------------------------------------------------------------
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
# ---------------------------------------------------------------------------
# Defines the networks to check and create if they do not exist
# ---------------------------------------------------------------------------

ZBX_TRAEFIK_NETWORK="$(grep '^ZBX_TRAEFIK_NETWORK[[:space:]]*=' .env | sed 's/^[^=]*=[[:space:]]*//')"
ZBX_GESTION_NETWORK="gestion_network"

# ---------------------------------------------------------------------------
# Create the helper scripts directory and set its permissions
# ---------------------------------------------------------------------------

USER_SCRIPTS_DIR="$REPO_ROOT/scripts/user_scripts"

if [ ! -d "$USER_SCRIPTS_DIR" ]; then
  echo "⚠️  User scripts directory not found. Creating: $USER_SCRIPTS_DIR"
  mkdir -p "$USER_SCRIPTS_DIR"
  if [ $? -ne 0 ]; then
    echo "❌  Error: could not create $USER_SCRIPTS_DIR"
    exit 1
  fi
fi

echo "✅  User scripts directory found: $USER_SCRIPTS_DIR"

chown root:root "$USER_SCRIPTS_DIR"
chmod 775 "$USER_SCRIPTS_DIR"

if compgen -G "$USER_SCRIPTS_DIR"/*.sh > /dev/null; then
  chown root:root "$USER_SCRIPTS_DIR"/*.sh
  chmod 775 "$USER_SCRIPTS_DIR"/*.sh

  # -------------------------------------------------------------------------
  # Also set permissions on Python scripts, if any exist
  # -------------------------------------------------------------------------
  chown root:root "$USER_SCRIPTS_DIR"/*.py 2>/dev/null
  chmod 775 "$USER_SCRIPTS_DIR"/*.py 2>/dev/null
  
  echo "✅  Permissions applied to $USER_SCRIPTS_DIR and *.sh files (root:root 775)"
else
  echo "⚠️  No .sh scripts found in $USER_SCRIPTS_DIR yet"
fi
# ---------------------------------------------------------------------------
# Create the agent sudoers file and set its permissions
# ---------------------------------------------------------------------------

AGENT_SUDOERS_FILE=$(grep '^AGENT_SUDOERS_FILE[[:space:]]*=' .env | sed 's/^[^=]*=[[:space:]]*//')

if [ -z "$AGENT_SUDOERS_FILE" ]; then
    echo "❌  Error: AGENT_SUDOERS_FILE is not set in .env"
    exit 1
fi

AGENT_SUDOERS_FILE_ROUTE=$REPO_ROOT/agent2/$AGENT_SUDOERS_FILE

if [ ! -f "$AGENT_SUDOERS_FILE_ROUTE" ]; then
    echo "⚠️  Sudoers file not found. Creating: $AGENT_SUDOERS_FILE_ROUTE"
    echo "zabbix ALL=(root) NOPASSWD: /var/lib/zabbix/user_scripts/*" > "$AGENT_SUDOERS_FILE_ROUTE"
    if [ $? -ne 0 ]; then
      echo "❌  Error: could not create $AGENT_SUDOERS_FILE_ROUTE"
      exit 1
    fi
fi

echo "✅  Sudoers file found: $AGENT_SUDOERS_FILE"
chown root:root "$AGENT_SUDOERS_FILE_ROUTE"
chmod 440 "$AGENT_SUDOERS_FILE_ROUTE"
echo "✅  Permissions applied to $AGENT_SUDOERS_FILE_ROUTE (root:root 440)"

# ---------------------------------------------------------------------------
# Create the agent Dockerfile if it does not already exist
# ---------------------------------------------------------------------------

DOCKERFILE="$REPO_ROOT/agent2/Dockerfile"

# ---------------------------------------------------------------------------
# Keep the heredoc block that generates the Dockerfile content contiguous
# ---------------------------------------------------------------------------

if [ ! -f "$DOCKERFILE" ]; then
    echo "⚠️  Dockerfile not found. Creating: $DOCKERFILE"
    cat > "$DOCKERFILE" << 'EOF'
FROM zabbix/zabbix-agent2:ubuntu-7.4.3
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends iproute2 && \
    rm -rf /var/lib/apt/lists/*
EOF
    if [ $? -ne 0 ]; then
      echo "❌  Error: could not create $DOCKERFILE"
      exit 1
    fi
fi
echo "✅  Dockerfile found: $DOCKERFILE"
chown root:root "$DOCKERFILE"
chmod 644 "$DOCKERFILE"
echo "✅  Permissions applied to $DOCKERFILE (root:root 644)"


# ---------------------------------------------------------------------------
# Create the agent configuration file if it is missing
# ---------------------------------------------------------------------------

ZBX_AGENT_CONFIG_FILE=$(grep '^ZBX_AGENT_CONFIG_FILE[[:space:]]*=' .env | sed 's/^[^=]*=[[:space:]]*//')

if [ -z "$ZBX_AGENT_CONFIG_FILE" ]; then
    echo "❌  Error: ZBX_AGENT_CONFIG_FILE is not set in .env"
    exit 1
fi

ZBX_AGENT_CONFIG_FILE_ROUTE=$REPO_ROOT/agent2/$ZBX_AGENT_CONFIG_FILE

if [ ! -f "$ZBX_AGENT_CONFIG_FILE_ROUTE" ]; then
    echo "⚠️  Zabbix agent config file not found. Creating: $ZBX_AGENT_CONFIG_FILE_ROUTE"
    touch "$ZBX_AGENT_CONFIG_FILE_ROUTE"
    if [ $? -ne 0 ]; then
      echo "❌  Error: could not create $ZBX_AGENT_CONFIG_FILE_ROUTE"
      exit 1
    fi
fi
echo "✅  Zabbix agent config file found: $ZBX_AGENT_CONFIG_FILE_ROUTE"

# ---------------------------------------------------------------------------
# Create the local plugins file from its example in agent mode only
# ---------------------------------------------------------------------------
# The actual file is excluded from the repository; each server keeps its own copy.
# Creating the file prevents Docker from mounting a directory and skipping the configuration.
# ---------------------------------------------------------------------------

if [ "$FLAG_SERVER" = "false" ]; then
  AGENT_PLUGINS_FILE_ROUTE=$REPO_ROOT/agent2/simovilab-plugins.conf

  if [ -d "$AGENT_PLUGINS_FILE_ROUTE" ]; then
      echo "❌  Error: $AGENT_PLUGINS_FILE_ROUTE is a directory (created by Docker during a previous deployment)."
      echo "    Remove it with: rmdir $AGENT_PLUGINS_FILE_ROUTE, then run the script again."
      exit 1
  fi

  if [ ! -f "$AGENT_PLUGINS_FILE_ROUTE" ]; then
      echo "⚠️  Plugins file not found. Creating from example: $AGENT_PLUGINS_FILE_ROUTE"
      if ! cp "$AGENT_PLUGINS_FILE_ROUTE.example" "$AGENT_PLUGINS_FILE_ROUTE"; then
        echo "❌  Error: could not create $AGENT_PLUGINS_FILE_ROUTE"
        exit 1
      fi
      echo -e "   - ${YELLOW}This file contains sample passwords. Edit it if this server uses different ones.${NC}"
  fi
  echo "✅  Plugins file found: $AGENT_PLUGINS_FILE_ROUTE"
  chmod 644 "$AGENT_PLUGINS_FILE_ROUTE"
fi

# ---------------------------------------------------------------------------
# Read AGENT_PARAMS_*=ITEMKEY:SCRIPTNAME entries defined in .env
# ---------------------------------------------------------------------------

echo "🔎  Checking for UserParameter entries in .env to add to $ZBX_AGENT_CONFIG_FILE..."

MATCHES=$(grep -E '^AGENT_PARAMS_[A-Za-z0-9_]+[[:space:]]*=' "$REPO_ROOT/.env")

if [ -z "$MATCHES" ]; then
  echo "ℹ️  No UserParameter entries found in .env, nothing to process"
else
  
  echo "$MATCHES" | while IFS= read -r line; do
  
    # -----------------------------------------------------------------------
    # Extract the configuration value from the line
    # -----------------------------------------------------------------------
    VALUE=$(echo "$line" | sed 's/^[^=]*=[[:space:]]*//')

    ITEMKEY=$(echo "$VALUE" | cut -d':' -f1)
    SCRIPTNAME=$(echo "$VALUE" | cut -d':' -f2)

    if [ -z "$ITEMKEY" ] || [ -z "$SCRIPTNAME" ]; then
      echo "⚠️  Skipping malformed line: $line on .env file"
      continue
    fi
    
    NEW_LINE="UserParameter=${ITEMKEY},sudo /var/lib/zabbix/user_scripts/${SCRIPTNAME}"
    
    # -----------------------------------------------------------------------
    # Avoid duplicate entries and exclude commented-out lines
    # -----------------------------------------------------------------------
    EXISTS=$(grep -v '^[[:space:]]*#' "$ZBX_AGENT_CONFIG_FILE_ROUTE" | grep -Fx "$NEW_LINE" || true)
    
    if [ -z "$EXISTS" ]; then
      echo "$NEW_LINE" >> "$ZBX_AGENT_CONFIG_FILE_ROUTE"
      echo "✅  Added: $NEW_LINE"
    else
      echo "ℹ️   Already present, skipping: $ITEMKEY"
    fi
  done
fi
chown root:root "$ZBX_AGENT_CONFIG_FILE_ROUTE"
chmod 644 "$ZBX_AGENT_CONFIG_FILE_ROUTE"
echo "✅  Permissions applied to $ZBX_AGENT_CONFIG_FILE_ROUTE (root:root 644)"

# ---------------------------------------------------------------------------
# Generate the PSK from a seed or a random value
# ---------------------------------------------------------------------------

mkdir -p psk

PSK_KDF_SALT="simovilab-zabbix-psk-v1"

if [ "$FLAG_SEED" = "true" ]; then
  echo "🔐 Custom seed detected, generating PSK from provided seed..."
  #PSK_VALUE=$(printf "%s" "$PSK_SEED" | openssl dgst -sha256 | cut -d' ' -f2)
  PSK_VALUE=$(python3 -c 'import hashlib,sys; print(hashlib.pbkdf2_hmac("sha256",
  sys.argv[1].encode(), sys.argv[2].encode(), 600000).hex())' \
  "$PSK_SEED" "$PSK_KDF_SALT")

  echo "$PSK_VALUE" > psk/zabbix_agentd.psk
  echo "   - ✅ Custom PSK generated."
  echo "   - 🚨  The same seed must be used for both the Zabbix server and remote agent deployment to generate matching PSK values."
else
  if [ "$FLAG_SERVER" = "true" ]; then
    echo "⚠️   No custom seed provided; generating a random PSK..."
    openssl rand -hex 32 > psk/zabbix_agentd.psk
    echo "   - ✅ Random PSK generated."
    echo -e "   - 🚨 Remember to use the same PSK value defined in the ${YELLOW}ZBX_TLSPSKFILENAME${NC} environment variable during both the Zabbix server and remote agent deployment."
  else
    echo "⚠️   No custom seed provided"
    echo -e "   - 🚨 Remember to use the same PSK value defined in the ${YELLOW}ZBX_TLSPSKFILENAME${NC} environment variable during both the Zabbix server and remote agent deployment."
  fi
fi

chmod 644 psk/zabbix_agentd.psk

# ---------------------------------------------------------------------------
# Install the timer that copies the server's memory and CPU to /hostfs
# ---------------------------------------------------------------------------
# The agent runs in Docker. Inside an LXC it reads the memory of its own
# container and the CPU and load of the Proxmox host. Every minute the timer
# writes the server's memory, CPU usage (cgroup) and pressure (PSI) to
# /srv/zabbix-disco, so the template reads the same correct values on bare metal,
# VM, cloud VM or LXC. Runs in server and agent mode: both deploy an agent.
# ---------------------------------------------------------------------------
HOST_STATS_DIR="$REPO_ROOT/scripts/host_stats"
install -m 755 "$HOST_STATS_DIR/simovilab-host-stats.sh" /usr/local/sbin/simovilab-host-stats.sh
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  install -m 644 "$HOST_STATS_DIR/simovilab-host-stats.service" /etc/systemd/system/simovilab-host-stats.service
  install -m 644 "$HOST_STATS_DIR/simovilab-host-stats.timer" /etc/systemd/system/simovilab-host-stats.timer
  systemctl daemon-reload
  systemctl enable --now simovilab-host-stats.timer >/dev/null
  systemctl start simovilab-host-stats.service
  if [ -s /srv/zabbix-disco/meminfo ]; then
    echo "✅  Host stats timer installed: /srv/zabbix-disco/meminfo is updated every minute"
  else
    echo "❌  Error: simovilab-host-stats did not write /srv/zabbix-disco/meminfo"
    echo "    Check: systemctl status simovilab-host-stats.service"
    exit 1
  fi
else
  echo -e "⚠️  ${YELLOW}systemd not found: the host stats timer was not installed.${NC}"
  echo "    Add this line to root's crontab (crontab -e) so memory and CPU items get data:"
  echo "    * * * * * /usr/local/sbin/simovilab-host-stats.sh /srv/zabbix-disco"
fi

# ---------------------------------------------------------------------------
# Checking required networks before running Compose
# ---------------------------------------------------------------------------
if [ "$FLAG_SERVER" = "true" ]; then
  echo "ℹ️  Server mode: checking required networks..."
  check_networks "$ZBX_GESTION_NETWORK" "$ZBX_TRAEFIK_NETWORK"
fi  


# ---------------------------------------------------------------------------
# Start the Docker Compose deployment and wait for the init result
# ---------------------------------------------------------------------------
echo -e "🚀  ${GREEN}Starting Docker Compose deployment...${NC}"
docker compose -f "$COMPOSE_FILE" --env-file "$REPO_ROOT/.env" up -d
if [ "$FLAG_SERVER" = "true" ]; then
  INIT_ID=$(docker compose -f "$COMPOSE_FILE" --env-file "$REPO_ROOT/.env" ps -aq zabbix-init)
  docker logs -f "$INIT_ID"
  INIT_RC=$(docker wait "$INIT_ID")
  if [ "$INIT_RC" = "0" ]; then
    echo -e "✅  ${GREEN}zabbix-init completed successfully${NC}"
  else
    echo -e "❌  zabbix-init failed (exit code $INIT_RC). Check the log above."
    exit "$INIT_RC"
  fi
fi