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

if [ -z "$AGENT_TEMPLATE_FILE" ]; then
  TEMPLATE_FILE=""
else
  TEMPLATE_FILE="/${AGENT_TEMPLATE_FILE}"
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
##############################    3. CREATE/UPDATE SIMOVILAB TEMPLATE  ######################################
#############################################################################################################


if [[ -f "$TEMPLATE_FILE" && -n "$AGENT_TEMPLATE_NAME" ]]; then
  echo -e "📋 Importing/updating the template: ${ORANGE}$AGENT_TEMPLATE_NAME${NC}"

  # ------------------------------------------------------------
  # Construir JSON usando jq
  # ------------------------------------------------------------

  IMPORT_REQUEST=$(jq -n \
      --arg source "$(cat "$TEMPLATE_FILE")" \
      '{
          jsonrpc: "2.0",
          method: "configuration.import",
          params: {
              format: "yaml",
              rules: {
                  templates: {
                      createMissing: true,
                      updateExisting: true
                  },
                  items: {
                      createMissing: true,
                      updateExisting: true
                  },
                  triggers: {
                      createMissing: true,
                      updateExisting: true
                  },
                  graphs: {
                      createMissing: true,
                      updateExisting: true
                  },
                  discoveryRules: {
                      createMissing: true,
                      updateExisting: true
                  },
                  valueMaps: {
                      createMissing: true,
                      updateExisting: true
                  }
              },
              source: $source
          },
          id: 1
      }')


  # ------------------------------------------------------------
  # Importar / actualizar template
  # ------------------------------------------------------------

  IMPORT_RESULT=$(curl -sf -X POST "$API_URL" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $TOKEN" \
      -d "$IMPORT_REQUEST")

  echo -e "📋 Import result: ${ORANGE}$IMPORT_RESULT${NC}"

  # ------------------------------------------------------------
  # Comprobar errores
  # ------------------------------------------------------------

  IMPORT_ERROR=$(echo "$IMPORT_RESULT" | jq -r '.error.message // empty')

  if [ -n "$IMPORT_ERROR" ]; then
      echo "❌ ERROR importing template:"
      echo -e "${RED}$IMPORT_RESULT${NC}"
      exit 1
  fi

  # ------------------------------------------------------------
  # Obtener TEMPLATE_ID
  # ------------------------------------------------------------

  TEMPLATE_ID=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"template.get\",\"params\":{\"filter\":{\"name\":[\"$AGENT_TEMPLATE_NAME\"]},\"output\":[\"templateid\"]},\"id\":1}" \
    | grep -o '"templateid":"[^"]*"' | head -1 | cut -d'"' -f4)

  if [ -z "$TEMPLATE_ID" ]; then
      echo "❌ ERROR: Could not obtain template ID. Something went wrong during the import process."
      echo -e "   ${YELLOW}- Check the ${BLUE}$AGENT_TEMPLATE_NAME${YELLOW} and ${BLUE}$AGENT_TEMPLATE_FILE${YELLOW} values in .env.${NC}"
      exit 1
  fi

  echo "✅ Template '$AGENT_TEMPLATE_NAME' ready with ID: $TEMPLATE_ID"
else
# -------------- Load default ICMP Ping template if the specified template file does not exist
  echo "⚠️  No templates found."
  echo -e "   ${YELLOW}- Check the ${BLUE}AGENT_TEMPLATE_NAME${YELLOW} and ${BLUE}AGENT_TEMPLATE_FILE${YELLOW} values in .env.${NC}"
  echo "⚠️  Using default ICMP Ping template."
  TEMPLATE_ID=$(curl -s -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"template.get\",\"params\":{\"filter\":{\"name\":[\"ICMP Ping\"]},\"output\":[\"templateid\"]},\"id\":1}" \
  | grep -o '"templateid":"[^"]*"' | head -1 | cut -d'"' -f4)
fi


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
else

# ── 4.3. If host does not exist — create it ───────────────────────────────
  echo "➕ Creating host: $LOCAL_HOST_NAME"
  curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"host.create\",\"params\":{\"host\":\"$LOCAL_HOST_NAME\",\"interfaces\":[{\"type\":1,\"main\":1,\"useip\":0,\"ip\":\"\",\"dns\":\"$HOST_DNS\",\"port\":\"$HOST_PORT\"}],\"groups\":[{\"groupid\":\"$GROUP_ID\"}],\"templates\":[{\"templateid\":\"$TEMPLATE_ID\"}]},\"id\":8}" > /dev/null
  echo "✅ Host created with DNS: $HOST_DNS:$HOST_PORT"
fi

#############################################################################################################
############################    5. CREATE AUTOREGISTRATION ACTION   #########################################
#############################################################################################################

# ── 5.1. Check if action already exists ───────────────────────────────────
ACTION_ID=$(curl -s -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"action.get\",\"params\":{\"filter\":{\"name\":[\"Self-registration-agents-simovilab\"]},\"output\":[\"actionid\"]},\"id\":1}" \
  | grep -o '"actionid":"[^"]*"' | head -1 | cut -d'"' -f4)

if [ -z "$ACTION_ID" ]; then

  # ── 5.2. If action does not exist — create it ───────────────────────────────
  echo "ℹ️  Creating autoregistration action for remote agents"

  # ── 5.2.1. Get groups IDs ──────────────────────────────────────────────────
  GROUP_ADD_ID=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"hostgroup.get\",\"params\":{\"filter\":{\"name\":[\"$HOST_GROUP\"]},\"output\":[\"groupid\"]},\"id\":1}" \
    | grep -o '"groupid":"[^"]*"' | head -1 | cut -d'"' -f4)

  GROUP_REMOVE_ID=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"hostgroup.get\",\"params\":{\"filter\":{\"name\":[\"Discovered hosts\"]},\"output\":[\"groupid\"]},\"id\":1}" \
    | grep -o '"groupid":"[^"]*"' | head -1 | cut -d'"' -f4)

  # ── 5.2.2. Get templates IDs ───────────────────────────────────────────────
  #TMPL_DOCKER_ID=$(curl -s -X POST "$API_URL" \
  #  -H "Content-Type: application/json" \
  #  -H "Authorization: Bearer $TOKEN" \
  #  -d "{\"jsonrpc\":\"2.0\",\"method\":\"template.get\",\"params\":{\"filter\":{\"name\":[\"Docker by Zabbix agent 2\"]},\"output\":[\"templateid\"]},\"id\":1}" \
  #  | grep -o '"templateid":"[^"]*"' | head -1 | cut -d'"' -f4)

  echo "   - 📁 Group to add   : $GROUP_ADD_ID"
  echo "   - 📁 Group to remove: $GROUP_REMOVE_ID"
  echo "   - 📋 Template to add: $TEMPLATE_ID"
  #echo "   - 📋 Linux template : $TMPL_LINUX_ID"

  #── 5.3. Create action ──────────────────────────────────────────────────
  curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"action.create\",\"params\":{\"name\":\"Self-registration-agents-simovilab\",\"eventsource\":2,\"status\":0,\"filter\":{\"evaltype\":0,\"conditions\":[{\"conditiontype\":24,\"operator\":2,\"value\":\"${ZBX_METADATA}\"}]},\"operations\":[{\"operationtype\":2},{\"operationtype\":4,\"opgroup\":[{\"groupid\":\"$GROUP_ADD_ID\"}]},{\"operationtype\":5,\"opgroup\":[{\"groupid\":\"$GROUP_REMOVE_ID\"}]},{\"operationtype\":6,\"optemplate\":[{\"templateid\":\"$TEMPLATE_ID\"}]}]},\"id\":1}" > /dev/null
  echo "   - ✅ Autoregistration action created"

  curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"autoregistration.update\",\"params\":{\"tls_accept\":2,\"tls_psk_identity\":\"$ZBX_TLSPSKIDENTITY\",\"tls_psk\":\"$PSK_VALUE\"},\"id\":1}" > /dev/null
  echo "   - 🔑 Autoregistration will use TLS PSK authentication"
  
  #curl -s -X POST "$API_URL" \
  #  -H "Content-Type: application/json" \
  #  -H "Authorization: Bearer $TOKEN" \
  #  -d "{\"jsonrpc\":\"2.0\",\"method\":\"action.create\",\"params\":{\"name\":\"Autoregistro-agentes-simovilab\",\"eventsource\":2,\"status\":0,\"filter\":{\"evaltype\":0,\"conditions\":[{\"conditiontype\":24,\"operator\":2,\"value\":\"docker-autoreg\"}]},\"operations\":[{\"operationtype\":2},{\"operationtype\":4,\"opgroup\":[{\"groupid\":\"$GROUP_ADD_ID\"}]},{\"operationtype\":5,\"opgroup\":[{\"groupid\":\"$GROUP_REMOVE_ID\"}]},{\"operationtype\":6,\"optemplate\":[{\"templateid\":\"$TMPL_DOCKER_ID\"},{\"templateid\":\"$TMPL_LINUX_ID\"}]},{\"operationtype\":9,\"optls\":{\"tls_connect\":2,\"tls_accept\":2,\"tls_psk_identity\":\"$ZBX_TLSPSKIDENTITY\",\"tls_psk\":\"$PSK_VALUE\"}}]},\"id\":1}" \
  #  echo "   - ✅ Autoregistration action created"

else
# ── 5.4. If action exists, do nothing ─────────────────────────────
  echo "ℹ️  Autoregistration action already exists"
fi

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