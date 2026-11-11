#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Preparar el entorno de desarrollo y desplegar los servicios de Zabbix
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Definir los colores usados en los mensajes del script
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
# Mostrar la sintaxis, las opciones y los requisitos de ejecución
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
# Imprimir un título de sección con el color indicado
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
# Validar que la semilla cumpla los requisitos de longitud y caracteres
# ---------------------------------------------------------------------------
validate_password() {
  local password="$1"
  # -------------------------------------------------------------------------
  # Elegir el nombre que se incluirá en los mensajes de error
  # -------------------------------------------------------------------------
  local var_name="${2:-seed}"
  local len=${#password}

  # -------------------------------------------------------------------------
  # Comprobar que la longitud esté entre 8 y 64 caracteres
  # -------------------------------------------------------------------------
  if (( len < 8 || len > 64 )); then
    echo "❌  Error: ${var_name} must be between 8 and 64 characters long (current: ${len})."
    return 1
  fi

  # -------------------------------------------------------------------------
  # Exigir al menos una letra mayúscula
  # -------------------------------------------------------------------------
  if [[ ! "$password" =~ [A-Z] ]]; then
    echo "❌  Error: ${var_name} must contain at least one uppercase letter."
    return 1
  fi

  # -------------------------------------------------------------------------
  # Exigir al menos una letra minúscula
  # -------------------------------------------------------------------------
  if [[ ! "$password" =~ [a-z] ]]; then
    echo "❌  Error: ${var_name} must contain at least one lowercase letter."
    return 1
  fi

  # -------------------------------------------------------------------------
  # Exigir al menos un número
  # -------------------------------------------------------------------------
  if [[ ! "$password" =~ [0-9] ]]; then
    echo "❌  Error: ${var_name} must contain at least one digit."
    return 1
  fi

  # -------------------------------------------------------------------------
  # Exigir al menos uno de los símbolos permitidos
  # -------------------------------------------------------------------------
  if [[ ! "$password" =~ [.*+?\-] ]]; then
    echo "❌  Error: ${var_name} must contain at least one of the following symbols: . * + ? -"
    return 1
  fi

  # -------------------------------------------------------------------------
  # Rechazar los caracteres que no están permitidos
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
# Detener la ejecución ante errores, variables sin definir o fallos de tubería
# ---------------------------------------------------------------------------
set -euo pipefail

# ---------------------------------------------------------------------------
# Cambiar al directorio raíz del repositorio desde cualquier ubicación
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Inicializar la selección del tipo de instalación
# ---------------------------------------------------------------------------
FLAG_SERVER=false

# ---------------------------------------------------------------------------
# Leer del segundo argumento la semilla PSK opcional
# ---------------------------------------------------------------------------
PSK_SEED="${2:-}"

# ---------------------------------------------------------------------------
# Validar la cantidad de argumentos recibidos
# ---------------------------------------------------------------------------
if [[ $# -ne 1 && $# -ne 2 ]]; then
  usage
  exit 1
fi
# ---------------------------------------------------------------------------
# Determinar si se desplegará el agente o la infraestructura del servidor
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
# Validar la semilla PSK personalizada cuando se proporcione
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
# Seleccionar el archivo de Compose según el tipo de instalación
# ---------------------------------------------------------------------------
COMPOSE_FILE="docker-compose.yml"
if [ "$FLAG_SERVER" = "false" ]; then
  COMPOSE_FILE="agent2/docker-compose-agent2.yml"
fi

# ---------------------------------------------------------------------------
# Obtener el identificador del grupo de Docker para el despliegue
# ---------------------------------------------------------------------------
DOCKER_GID=$(getent group docker | cut -d: -f3)
if [ -z "$DOCKER_GID" ]; then
    echo "❌  Docker group not found."
    exit 1
fi
export DOCKER_GID
echo "🐳  Docker GID: $DOCKER_GID"

# ---------------------------------------------------------------------------
# Comprobar e instalar las dependencias necesarias
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Verificar que Docker esté instalado y disponible
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "❌  Error: docker is not installed or not in PATH."
    exit 1
fi
echo "✅  Docker ready: $(docker --version | cut -d' ' -f1-3)"

# ---------------------------------------------------------------------------
# Instalar curl si falta y verificar que esté disponible
# ---------------------------------------------------------------------------
if ! command -v curl &> /dev/null; then
  echo "🔧  Installing curl..."
  command -v apt-get &> /dev/null && apt-get update -qq && apt-get install -y -qq curl
fi
command -v curl &> /dev/null || { echo "❌  Error: curl is not installed or not in PATH."; exit 1; }
echo "✅  curl ready: $(curl -V | head -n1 | cut -d' ' -f1-2)"

# ---------------------------------------------------------------------------
# Instalar OpenSSL si falta y verificar que esté disponible
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
# Comprobar los archivos requeridos para el despliegue
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Verificar la existencia del archivo de variables de entorno
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
# Verificar la existencia del archivo de Docker Compose seleccionado
# ---------------------------------------------------------------------------
if [ ! -f "$COMPOSE_FILE" ]; then
    echo "❌  Error: $COMPOSE_FILE not found."
    exit 1
fi
echo "✅  Docker Compose file found: $COMPOSE_FILE"

# ---------------------------------------------------------------------------
# Verificar la configuración del script posterior a la instalación del servidor
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
# Crear el directorio de scripts auxiliares y asignar sus permisos
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
  # Aplicar también los permisos a los scripts Python, si existen
  # -------------------------------------------------------------------------
  chown root:root "$USER_SCRIPTS_DIR"/*.py 2>/dev/null
  chmod 775 "$USER_SCRIPTS_DIR"/*.py 2>/dev/null
  
  echo "✅  Permissions applied to $USER_SCRIPTS_DIR and *.sh files (root:root 775)"
else
  echo "⚠️  No .sh scripts found in $USER_SCRIPTS_DIR yet"
fi
# ---------------------------------------------------------------------------
# Crear y asegurar los permisos del archivo sudoers del agente
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
# Crear el Dockerfile del agente si todavía no existe
# ---------------------------------------------------------------------------

DOCKERFILE="$REPO_ROOT/agent2/Dockerfile"

# ---------------------------------------------------------------------------
# Mantener contiguo el bloque heredoc que genera el contenido del Dockerfile
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
# Crear el archivo de configuración del agente si falta
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
# Crear desde el ejemplo el archivo local de plugins, solo para el modo agente
# ---------------------------------------------------------------------------
# El archivo real se excluye del repositorio y cada servidor mantiene su copia.
# Crear el archivo evita que Docker monte una carpeta y omita la configuración.
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
# Leer las entradas AGENT_PARAMS_*=ITEMKEY:SCRIPTNAME definidas en .env
# ---------------------------------------------------------------------------

echo "🔎  Checking for UserParameter entries in .env to add to $ZBX_AGENT_CONFIG_FILE..."

MATCHES=$(grep -E '^AGENT_PARAMS_[A-Za-z0-9_]+[[:space:]]*=' "$REPO_ROOT/.env")

if [ -z "$MATCHES" ]; then
  echo "ℹ️  No UserParameter entries found in .env, nothing to process"
else
  
  echo "$MATCHES" | while IFS= read -r line; do
  
    # -----------------------------------------------------------------------
    # Separar el valor de configuración de la línea leída
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
    # Evitar duplicar la entrada y excluir las líneas comentadas
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
# Generar la clave PSK desde una semilla o mediante un valor aleatorio
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
# Iniciar el despliegue con Docker Compose y esperar el resultado del init
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