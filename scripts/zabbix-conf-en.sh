#!/usr/bin/env bash
########################################################################################################################
###### Script post-install for Zabbix, executed by the zabbix-init service, defined in docker-compose.yml #############
########################################################################################################################
# Colors
ORANGE='\033[38;2;252;209;161m'
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m'
#──── Exit on error, e=exit on error, u=exit on undefined var ──────────
set -eu
#──── Flag to determine if a new user should be created with API_WEB_USER and API_WEB_PASS from .env *** DO NOT CHANGE THE VALUE *** ──────────
flag_new_user=FALSE

#----- PSK file inside the container, used to read the PSK value and configure API autoregistration settings
PSK_VALUE=$(cat /${ZBX_TLSPSKFILENAME})

#──── Recorta espacios al inicio y al final, sin depender de utilidades externas
trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"; printf '%s' "$s"; }

#──── Guarda de migración: avisa si quedaron las variables antiguas
if [ -n "${AGENT_TEMPLATE_FILE:-}${AGENT_TEMPLATE_FILES:-}" ]; then
  echo "❌ ERROR: AGENT_TEMPLATE_FILE(S) / AGENT_TEMPLATE_NAME(S) quedaron obsoletas."
  echo "   Use LOCAL_TEMPLATE_*, ROLE_DATABUS_TEMPLATE_* y ROLE_INFOBUS_TEMPLATE_* (ver .env.example)."
  exit 1
fi
#############################################################################################################
#######################################    0. IS API AVAILABLE?   ###########################################
#############################################################################################################
echo "⏳ Waiting for Zabbix API to be available..."
MAX_RETRIES=10      
COUNT=0

until curl -sf -o /dev/null -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"apiinfo.version","params":{},"id":1}'; do
  
  COUNT=$((COUNT + 1))
  
  if [ "$COUNT" -ge "$MAX_RETRIES" ]; then
    echo "❌ API not available after $MAX_RETRIES attempts, exiting..."
    exit 1
  fi
  
  echo "⏳ Attempt $COUNT/$MAX_RETRIES — retrying in 5 seconds..."
  sleep 5
done

echo "✅ API available"

#############################################################################################################
##################################    0.5. GENERATE TLS CERTIFICATES  ######################################
#############################################################################################################



#############################################################################################################
##########################################    1. AUTHENTICATE   #############################################
#############################################################################################################


# ── 1.1. Authenticating as default ─────────────────────────────────
echo "🔒 Authenticating with default user..."
TOKEN=$(curl -sf -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"user.login\",\"params\":{\"username\":\"$DEFAULT_USER\",\"password\":\"$DEFAULT_PASS\"},\"id\":1}" \
  | grep -o '"result":"[^"]*"' | cut -d'"' -f4)

# ── 1.2. Authenticating as '$API_WEB_USER' ─────────────────────────────────
if [ -z "$TOKEN" ]; then
  echo "⚠️  Authentication failed with default user, trying with user '$API_WEB_USER' defined in .env..."
  TOKEN=$(curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"user.login\",\"params\":{\"username\":\"$API_WEB_USER\",\"password\":\"$API_WEB_PASS\"},\"id\":1}" \
    | grep -o '"result":"[^"]*"' | cut -d'"' -f4)

  if [ -z "$TOKEN" ]; then
    echo "❌ Could not authenticate with any user, exiting..."
    exit 1
  fi
  echo "   - 🔑 Authenticated with .env user: $API_WEB_USER"
else
  echo "   - 🔑 Authenticated with default user: $DEFAULT_USER"
  flag_new_user=TRUE
fi
echo "   - 🔑 Token obtained successfully"

#############################################################################################################
###############################    2. GET OR CREATE HOST GROUP    ############################################
#############################################################################################################

GROUP_ID=$(curl -s -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"hostgroup.get\",\"params\":{\"filter\":{\"name\":[\"$HOST_GROUP\"]},\"output\":[\"groupid\"]},\"id\":1}" \
  | grep -o '"groupid":"[^"]*"' | head -1 | cut -d'"' -f4)

if [ -z "$GROUP_ID" ]; then
  echo "📁 Creating group: $HOST_GROUP"
  GROUP_ID=$(curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"hostgroup.create\",\"params\":{\"name\":\"$HOST_GROUP\"},\"id\":3}" \
    | grep -o '"groupids":\["[^"]*"' | cut -d'"' -f4)
else
  echo "📁 Group \"$HOST_GROUP\" already exists."
fi
echo "   - 📁 Groupname: '$HOST_GROUP' ID: $GROUP_ID"

#############################################################################################################
##############################    3. CREATE/UPDATE SIMOVILAB TEMPLATES  #####################################
#############################################################################################################
# Tres listas de plantillas, cada una con su par FILES (coma) / NAMES (barra vertical), en el mismo orden.
# El separador de nombres no puede ser coma: los nombres de plantilla de Zabbix la contienen.
#
#   LOCAL_TEMPLATE_*         -> host local de R4 (contenedor zabbix-agent2 del servidor)
#   ROLE_DATABUS_TEMPLATE_*  -> agentes remotos cuyo ZBX_METADATA contiene "databus" (R3)
#   ROLE_INFOBUS_TEMPLATE_*  -> agentes remotos cuyo ZBX_METADATA contiene "infobus" (R2)

declare -A IMPORTED_IDS=()   # archivo -> templateid; evita importar dos veces el mismo archivo
_TPL_ID=""
TPL_JSON="[]"

import_template() {
  local _file="$1" _name="$2" _id _req _res _err

  #── Ya importado en esta corrida: reutiliza el ID
  if [ -n "${IMPORTED_IDS[$_file]:-}" ]; then
    _TPL_ID="${IMPORTED_IDS[$_file]}"
    return 0
  fi

  if [ ! -f "$_file" ]; then
    echo -e "❌ ERROR: no existe el archivo de plantilla ${BLUE}${_file}${NC}"
    exit 1
  fi

  echo -e "📋 Importando/actualizando: ${ORANGE}${_name}${NC}  (${_file})"

  _req=$(jq -n --arg source "$(cat "$_file")" \
      '{
          jsonrpc: "2.0",
          method: "configuration.import",
          params: {
              format: "yaml",
              rules: {
                  template_groups: { createMissing: true, updateExisting: true },
                  templates:       { createMissing: true, updateExisting: true },
                  items:           { createMissing: true, updateExisting: true },
                  triggers:        { createMissing: true, updateExisting: true },
                  graphs:          { createMissing: true, updateExisting: true },
                  discoveryRules:  { createMissing: true, updateExisting: true },
                  valueMaps:       { createMissing: true, updateExisting: true },
                  templateDashboards: { createMissing: true, updateExisting: true }
              },
              source: $source
          },
          id: 1
      }')

  _res=$(curl -sf -X POST "$API_URL" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $TOKEN" \
      -d "$_req")

  _err=$(echo "$_res" | jq -r '.error.data // .error.message // empty')
  if [ -n "$_err" ]; then
      echo "❌ ERROR importando la plantilla '${_name}':"
      echo -e "${RED}${_err}${NC}"
      exit 1
  fi

  _id=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "$(jq -n --arg n "$_name" \
        '{jsonrpc:"2.0",method:"template.get",params:{filter:{name:[$n]},output:["templateid"]},id:1}')" \
    | jq -r '.result[0].templateid // empty')

  if [ -z "$_id" ]; then
      echo "❌ ERROR: no se obtuvo el ID de la plantilla '${_name}'."
      echo -e "   ${YELLOW}El nombre en .env tiene que ser idéntico al campo 'name:' de ${BLUE}${_file}${NC}"
      exit 1
  fi

  echo "   - ✅ '${_name}' lista, ID ${_id}"
  IMPORTED_IDS[$_file]="$_id"
  _TPL_ID="$_id"
}
#──── lookup_template NOMBRE -> plantilla que ya existe en Zabbix (oficial), sin archivo que importar
lookup_template() {
  local _name="$1" _id
  echo -e "📋 Plantilla existente en Zabbix: ${ORANGE}${_name}${NC}"

  _id=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "$(jq -n --arg n "$_name" \
        '{jsonrpc:"2.0",method:"template.get",params:{filter:{name:[$n]},output:["templateid"]},id:1}')" \
    | jq -r '.result[0].templateid // empty')

  if [ -z "$_id" ]; then
      echo "❌ ERROR: Zabbix no tiene una plantilla llamada '${_name}'."
      echo -e "   ${YELLOW}Copie el nombre exacto desde Data collection → Templates${NC}"
      exit 1
  fi

  echo "   - ✅ '${_name}' lista, ID ${_id}"
  _TPL_ID="$_id"
}

#──── resolve_templates FILES NAMES ETIQUETA -> deja el arreglo JSON en TPL_JSON
resolve_templates() {
  local _files="$1" _names="$2" _label="$3" _ids="" _i
  local -a _tpl_files _tpl_names

  if [ -z "$_files" ] || [ -z "$_names" ]; then
    TPL_JSON="[]"
    return 0
  fi

  IFS=',' read -r -a _tpl_files <<< "$_files"
  IFS='|' read -r -a _tpl_names <<< "$_names"

  if [ "${#_tpl_files[@]}" -ne "${#_tpl_names[@]}" ]; then
    echo "❌ ERROR (${_label}): hay ${#_tpl_files[@]} archivos y ${#_tpl_names[@]} nombres."
    echo -e "   ${YELLOW}Archivos separados por coma, nombres por barra vertical, y en el mismo orden.${NC}"
    exit 1
  fi

  for _i in "${!_tpl_files[@]}"; do
    if [ "$(trim "${_tpl_files[$_i]}")" = "-" ]; then
      lookup_template "$(trim "${_tpl_names[$_i]}")"
    else
      import_template "/templates/$(trim "${_tpl_files[$_i]}")" "$(trim "${_tpl_names[$_i]}")"
    fi
    _ids="${_ids}${_ids:+ }${_TPL_ID}"
  done

  TPL_JSON=$(printf '%s\n' $_ids | jq -R '{templateid: .}' | jq -s -c '.')
  echo "   - 📋 Plantillas para ${_label}: $TPL_JSON"
}

#──── 3.1. Host local (R4)
resolve_templates "${LOCAL_TEMPLATE_FILES:-}" "${LOCAL_TEMPLATE_NAMES:-}" "host local"
LOCAL_TEMPLATE_JSON="$TPL_JSON"

if [ "$LOCAL_TEMPLATE_JSON" = "[]" ]; then
  echo "⚠️  No se indicaron plantillas para el host local; se usa ICMP Ping."
  _icmp=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","method":"template.get","params":{"filter":{"name":["ICMP Ping"]},"output":["templateid"]},"id":1}' \
    | jq -r '.result[0].templateid // empty')
  LOCAL_TEMPLATE_JSON=$(jq -n -c --arg id "$_icmp" '[{templateid: $id}]')
fi

#──── 3.2. Roles de agentes remotos (se vinculan por autorregistro, sección 5)
resolve_templates "${ROLE_DATABUS_TEMPLATE_FILES:-}" "${ROLE_DATABUS_TEMPLATE_NAMES:-}" "rol databus"
DATABUS_TEMPLATE_JSON="$TPL_JSON"
resolve_templates "${ROLE_INFOBUS_TEMPLATE_FILES:-}" "${ROLE_INFOBUS_TEMPLATE_NAMES:-}" "rol infobus"
INFOBUS_TEMPLATE_JSON="$TPL_JSON"

#############################################################################################################
############################    4. CHECK IF LOCAL AGENT2 EXISTS   ###########################################
#############################################################################################################

# ── 4.1. Check if host already exists ───────────────────────────────────
HOST_ID=$(curl -sf -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"host.get\",\"params\":{\"filter\":{\"host\":[\"$LOCAL_HOST_NAME\"]}},\"id\":5}" \
  | grep -o '"hostid":"[^"]*"' | head -1 | cut -d'"' -f4)

# ── 4.2. If host exists — update interface ────────────────────────────────
if [ -n "$HOST_ID" ]; then
    echo "🔄 Host already exists (ID: $HOST_ID), updating interface..."

# ── 4.2.1 Get current interface ────────────────────────────────────────────
  IFACE_ID=$(curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"hostinterface.get\",\"params\":{\"hostids\":\"$HOST_ID\"},\"id\":6}" \
    | grep -o '"interfaceid":"[^"]*"' | head -1 | cut -d'"' -f4)

# ── 4.2.2. Validate IFACE_ID ──────────────────────────────────────────────
  if [ -z "$IFACE_ID" ]; then
    echo "❌ No interface found for host $HOST_ID, exiting..."
    exit 1
  fi

# ── 4.2.3. Update interface to DNS ──────────────────────────────────────────
  curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"hostinterface.update\",\"params\":{\"interfaceid\":\"$IFACE_ID\",\"type\":1,\"useip\":0,\"dns\":\"$HOST_DNS\",\"ip\":\"\",\"port\":\"$HOST_PORT\",\"main\":1},\"id\":7}" > /dev/null
  echo "   - ✅ Interface updated to DNS: $HOST_DNS:$HOST_PORT"
# ── 4.2.4. Revincular plantillas: sin esto, una segunda corrida no aplica una plantilla nueva al host que ya existe.
#           Las que ya no estén en LOCAL_TEMPLATE_* se desvinculan Y se limpian (templates_clear);
#           con solo desvincular, sus ítems quedarían pegados al host.
  _linked=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"host.get\",\"params\":{\"hostids\":\"$HOST_ID\",\"selectParentTemplates\":[\"templateid\"],\"output\":[\"hostid\"]},\"id\":9}" \
    | jq -c '[.result[0].parentTemplates[]? | {templateid}]')
  _clear=$(jq -n -c --argjson have "$_linked" --argjson want "$LOCAL_TEMPLATE_JSON" '$have - $want')

  curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"host.update\",\"params\":{\"hostid\":\"$HOST_ID\",\"templates\":$LOCAL_TEMPLATE_JSON,\"templates_clear\":$_clear},\"id\":9}" > /dev/null
  echo "   - ✅ Plantillas revinculadas: $LOCAL_TEMPLATE_JSON"
  [ "$_clear" != "[]" ] && echo "   - 🧹 Plantillas desvinculadas y limpiadas: $_clear"
else

# ── 4.3. If host does not exist — create it ───────────────────────────────
  echo "➕ Creating host: $LOCAL_HOST_NAME"
  curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"host.create\",\"params\":{\"host\":\"$LOCAL_HOST_NAME\",\"interfaces\":[{\"type\":1,\"main\":1,\"useip\":0,\"ip\":\"\",\"dns\":\"$HOST_DNS\",\"port\":\"$HOST_PORT\"}],\"groups\":[{\"groupid\":\"$GROUP_ID\"}],\"templates\":$LOCAL_TEMPLATE_JSON},\"id\":8}" > /dev/null
  echo "✅ Host created with DNS: $HOST_DNS:$HOST_PORT"
fi
# ──── 4.4 Delete the default "Zabbix server" agent ───────────────────────────
DEFAULT_HOST_ID=$(curl -sf -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"host.get\",\"params\":{\"filter\":{\"host\":[\"Zabbix server\"]}},\"id\":5}" \
  | grep -o '"hostid":"[^"]*"' | head -1 | cut -d'"' -f4)

curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"host.delete\",\"params\":[\"$DEFAULT_HOST_ID\"],\"id\":7}"

#############################################################################################################
############################    5. CREATE AUTOREGISTRATION ACTION   #########################################
#############################################################################################################
# Una acción por rol. Cada agente remoto declara su rol en ZBX_METADATA (p. ej. "docker-autoreg databus").
# La acción exige las DOS condiciones (evaltype 1 = AND): la metadata común y el rol.
# El host local de R4 usa solo "docker-autoreg", así que no cae en ninguna acción.

GROUP_ADD_ID=$(curl -s -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"hostgroup.get\",\"params\":{\"filter\":{\"name\":[\"$HOST_GROUP\"]},\"output\":[\"groupid\"]},\"id\":1}" \
  | jq -r '.result[0].groupid // empty')

GROUP_REMOVE_ID=$(curl -s -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","method":"hostgroup.get","params":{"filter":{"name":["Discovered hosts"]},"output":["groupid"]},"id":1}' \
  | jq -r '.result[0].groupid // empty')

echo "   - 📁 Group to add   : $GROUP_ADD_ID"
echo "   - 📁 Group to remove: $GROUP_REMOVE_ID"

# ── 5.1. Borrar la acción única anterior: vinculaba la misma lista a TODOS los agentes ──
LEGACY_ACTION_ID=$(curl -s -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","method":"action.get","params":{"filter":{"name":["Self-registration-agents-simovilab"]},"output":["actionid"]},"id":1}' \
  | jq -r '.result[0].actionid // empty')

if [ -n "$LEGACY_ACTION_ID" ]; then
  curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"action.delete\",\"params\":[\"$LEGACY_ACTION_ID\"],\"id\":1}" > /dev/null
  echo "   - 🗑️  Acción anterior 'Self-registration-agents-simovilab' eliminada"
fi

# ── 5.2. Crear o actualizar la acción de un rol ──────────────────────────────
upsert_role_action() {
  local _role="$1" _tpl_json="$2" _name="Self-registration-simovilab-$1" _action_id _body

  if [ "$_tpl_json" = "[]" ]; then
    echo "⚠️  Rol '${_role}' sin plantillas en .env: no se crea su acción de autorregistro."
    return 0
  fi

  _body=$(jq -n -c \
    --arg meta "$ZBX_METADATA" --arg role "$_role" \
    --arg gadd "$GROUP_ADD_ID" --arg grem "$GROUP_REMOVE_ID" \
    --argjson tpl "$_tpl_json" \
    '{filter:{evaltype:1,conditions:[
        {conditiontype:24,operator:2,value:$meta},
        {conditiontype:24,operator:2,value:$role}]},
      operations:[
        {operationtype:2},
        {operationtype:4,opgroup:[{groupid:$gadd}]},
        {operationtype:5,opgroup:[{groupid:$grem}]},
        {operationtype:6,optemplate:$tpl}]}')

  _action_id=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "$(jq -n -c --arg n "$_name" '{jsonrpc:"2.0",method:"action.get",params:{filter:{name:[$n]},output:["actionid"]},id:1}')" \
    | jq -r '.result[0].actionid // empty')

  if [ -z "$_action_id" ]; then
    curl -s -X POST "$API_URL" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $TOKEN" \
      -d "$(jq -n -c --arg n "$_name" --argjson b "$_body" '{jsonrpc:"2.0",method:"action.create",params:({name:$n,eventsource:2,status:0}+$b),id:1}')" > /dev/null
    echo "   - ✅ Acción '${_name}' creada: $_tpl_json"
  else
    # Se actualiza siempre, para que un cambio en .env llegue a la acción en la siguiente corrida
    curl -s -X POST "$API_URL" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $TOKEN" \
      -d "$(jq -n -c --arg id "$_action_id" --argjson b "$_body" '{jsonrpc:"2.0",method:"action.update",params:({actionid:$id}+$b),id:1}')" > /dev/null
    echo "   - 🔄 Acción '${_name}' actualizada: $_tpl_json"
  fi
}

echo "ℹ️  Configuring autoregistration actions for remote agents"
upsert_role_action databus "$DATABUS_TEMPLATE_JSON"
upsert_role_action infobus "$INFOBUS_TEMPLATE_JSON"

# ── 5.3. PSK del autorregistro ───────────────────────────────────────────────
curl -s -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"autoregistration.update\",\"params\":{\"tls_accept\":2,\"tls_psk_identity\":\"$ZBX_TLSPSKIDENTITY\",\"tls_psk\":\"$PSK_VALUE\"},\"id\":1}" > /dev/null
echo "   - 🔑 Autoregistration will use TLS PSK authentication"

#############################################################################################################
#############   6. CREATE NEW USER — only when authenticated with default credentials   #####################
#############################################################################################################

# ── 6.1. Get Super Admin role ID ──────────────────────────────────────────
if [ "$flag_new_user" = "TRUE" ]; then
  echo "ℹ️  Creating new user '$API_WEB_USER' and removing default account"
  echo "🔍 Getting Super Admin role..."
  ROLE_ID=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","method":"role.get","params":{"filter":{"name":["Super admin role"]},"output":["roleid"]},"id":1}' \
    | grep -o '"roleid":"[^"]*"' | head -1 | cut -d'"' -f4)
  
# ── 6.1.1. Validate ROLE_ID ───────────────────────────────────────────────
  if [ -z "$ROLE_ID" ]; then
    echo "❌ Super Admin role not found, exiting..."
    exit 1
  fi
  echo "   - 👑 Role ID: $ROLE_ID"

# ── 6.2. Get Zabbix administrators group ID ───────────────────────────────
  echo "🔍 Getting 'Zabbix administrators' group ID..."
  USERGROUP_ID_0=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","method":"usergroup.get","params":{"filter":{"name":["Zabbix administrators"]},"output":["usrgrpid"]},"id":1}' \
    | grep -o '"usrgrpid":"[^"]*"' | head -1 | cut -d'"' -f4)

# ── 6.2.1. Validate USERGROUP_ID_0 ───────────────────────────────────────
  if [ -z "$USERGROUP_ID_0" ]; then
    echo "❌ Group 'Zabbix administrators' not found, exiting..."
    exit 1
  fi
  echo "   - 👥 'Zabbix administrators' usergroup ID: $USERGROUP_ID_0"

# ── 6.3. Get Internal group ID ────────────────────────────────────────────
  echo "🔍 Getting 'Internal' group ID..."
  USERGROUP_ID_1=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","method":"usergroup.get","params":{"filter":{"name":["Internal"]},"output":["usrgrpid"]},"id":1}' \
    | grep -o '"usrgrpid":"[^"]*"' | head -1 | cut -d'"' -f4)

# ── 6.3.1. Validate USERGROUP_ID_1 ───────────────────────────────────────
  if [ -z "$USERGROUP_ID_1" ]; then
    echo "❌ Group 'Internal' not found, exiting..."
    exit 1
  fi
  echo "   - 👥 'Internal' usergroup ID: $USERGROUP_ID_1"

# ── 6.4. Create new user ──────────────────────────────────────────────────
# ── 6.4.1. Check if user already exists ──────────────────────────────────
  NEW_USER_ID=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"user.get\",\"params\":{\"filter\":{\"username\":[\"$API_WEB_USER\"]},\"output\":[\"userid\"]},\"id\":1}" \
    | grep -o '"userid":"[^"]*"' | head -1 | cut -d'"' -f4)

# ── 6.4.2. If exists — skip creation ─────────────────────────────────────
  if [ -n "$NEW_USER_ID" ]; then
    echo "ℹ️  User '$API_WEB_USER' already exists"
  else
# ── 6.4.3. If does not exist — create it ─────────────────────────────────
    echo "➕ Creating user: $API_WEB_USER..."
    NEW_USER_ID=$(curl -s -X POST "$API_URL" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $TOKEN" \
      -d "{\"jsonrpc\":\"2.0\",\"method\":\"user.create\",\"params\":{\"username\":\"$API_WEB_USER\",\"passwd\":\"$API_WEB_PASS\",\"roleid\":\"$ROLE_ID\",\"usrgrps\":[{\"usrgrpid\":\"$USERGROUP_ID_0\"},{\"usrgrpid\":\"$USERGROUP_ID_1\"}],\"name\":\"$API_WEB_USER\",\"autologin\":0,\"autologout\":\"0\"},\"id\":1}" \
      | grep -o '"userids":\["[^"]*"' | cut -d'"' -f4)

# ── 6.4.4. Validate NEW_USER_ID ──────────────────────────────────────────
    if [ -z "$NEW_USER_ID" ]; then
      echo "❌ Something went wrong creating user $API_WEB_USER, default account will NOT be deleted..."
      echo "🛡️  Verify that the password in .env meets Zabbix security policies:"
      echo "   - 🔐 Password requirements:"
      echo "      - must be at least 8 characters long"
      echo "      - must not contain user's name, surname or username"
      echo "      - must not be one of common or context-specific passwords"
      exit 1
    fi
    echo "   - ✅ User $API_WEB_USER created with ID: $NEW_USER_ID"
  fi

#############################################################################################################
#####################################   7. DELETE DEFAULT ACCOUNT   #########################################
#############################################################################################################

# ── 7.1. Login with new user ──────────────────────────────────────────────
  echo "   - 🔑 Authenticating with new user..."
  NEW_TOKEN=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"user.login\",\"params\":{\"username\":\"$API_WEB_USER\",\"password\":\"$API_WEB_PASS\"},\"id\":1}" \
    | grep -o '"result":"[^"]*"' | cut -d'"' -f4)

# ── 7.1.1. Validate NEW_TOKEN ────────────────────────────────────────────
  if [ -z "$NEW_TOKEN" ]; then
    echo "⚠️  Could not authenticate with new user, exiting..."
    exit 1
  fi

  echo "   - 🔑 New user token obtained successfully"
  echo "ℹ️  Proceeding to delete default account"

# ── 7.2. Get Admin user ID ────────────────────────────────────────────────
  echo "🔍 Looking up Admin user..."
  ADMIN_ID=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $NEW_TOKEN" \
    -d '{"jsonrpc":"2.0","method":"user.get","params":{"filter":{"username":["Admin"]},"output":["userid"]},"id":1}' \
    | grep -o '"userid":"[^"]*"' | head -1 | cut -d'"' -f4)

# ── 7.2.1. Validate ADMIN_ID ─────────────────────────────────────────────
  if [ -z "$ADMIN_ID" ]; then
    echo "⚠️  Admin user not found, may have already been deleted, exiting..."
    exit 0
  fi
  echo "   - 🔍 Admin ID: $ADMIN_ID"

# ── 7.2.2. Transfer maps from Admin to new user ───────────────────────────
  echo "🗺️  Transferring maps from 'Admin' to new user '$API_WEB_USER'..."
  MAPS=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $NEW_TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"map.get\",\"params\":{\"filter\":{\"userid\":[\"$ADMIN_ID\"]},\"output\":[\"sysmapid\"]},\"id\":1}" \
    | grep -o '"sysmapid":"[^"]*"' | cut -d'"' -f4)

  for MAP_ID in $MAPS; do
    curl -s -X POST "$API_URL" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $NEW_TOKEN" \
      -d "{\"jsonrpc\":\"2.0\",\"method\":\"map.update\",\"params\":{\"sysmapid\":\"$MAP_ID\",\"userid\":\"$NEW_USER_ID\"},\"id\":1}" > /dev/null
    echo "   - 🗺️  Map '$MAP_ID' transferred"
  done

# ── 7.2.3. Transfer dashboards from Admin to new user ────────────────────
  echo "📊  Transferring dashboards from Admin to new user '$API_WEB_USER'..."
  DASHBOARDS=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $NEW_TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"dashboard.get\",\"params\":{\"filter\":{\"userid\":[\"$ADMIN_ID\"]},\"output\":[\"dashboardid\"]},\"id\":1}" \
    | grep -o '"dashboardid":"[^"]*"' | cut -d'"' -f4)

  for DASH_ID in $DASHBOARDS; do
    curl -s -X POST "$API_URL" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $NEW_TOKEN" \
      -d "{\"jsonrpc\":\"2.0\",\"method\":\"dashboard.update\",\"params\":{\"dashboardid\":\"$DASH_ID\",\"userid\":\"$NEW_USER_ID\"},\"id\":1}" > /dev/null
    echo "   - 📊  Dashboard '$DASH_ID' transferred"
  done

# ── 7.2.4. Delete Admin user ──────────────────────────────────────────────
  echo "🗑️  Deleting Admin user..."
  curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $NEW_TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"user.delete\",\"params\":[\"$ADMIN_ID\"],\"id\":1}" > /dev/null

  echo "✅ User 'Admin' deleted"
  echo "✅ User configuration completed"
  echo "   - 👨‍💻 Active user: $API_WEB_USER"
else
  echo "ℹ️  No new user required"
  echo "   - 👨‍💻 Active user: $API_WEB_USER"
fi

echo "🎉 Initialization complete"