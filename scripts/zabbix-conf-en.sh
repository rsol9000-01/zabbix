#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Configure Zabbix after installation through the zabbix-init service
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Define the colors used in script messages
# ---------------------------------------------------------------------------
ORANGE='\033[38;2;252;209;161m'
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m'
# ---------------------------------------------------------------------------
# Stop execution on errors or unset variables
# ---------------------------------------------------------------------------
set -eu
# ---------------------------------------------------------------------------
# Initialize the flag for creating the web user with the .env credentials
# TRUE indicates that login used the default credentials.
# ---------------------------------------------------------------------------
flag_new_user=FALSE

# ---------------------------------------------------------------------------
# Read the PSK used for Zabbix autoregistration from the mounted file
# ---------------------------------------------------------------------------
PSK_VALUE=$(cat /${ZBX_TLSPSKFILENAME})

# ---------------------------------------------------------------------------
# Trim leading and trailing whitespace without relying on external utilities
# ---------------------------------------------------------------------------
trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"; printf '%s' "$s"; }

# ---------------------------------------------------------------------------
# Reject configuration variables that are no longer supported
# ---------------------------------------------------------------------------
if [ -n "${AGENT_TEMPLATE_FILE:-}${AGENT_TEMPLATE_FILES:-}" ]; then
  echo "❌ ERROR: AGENT_TEMPLATE_FILE(S) / AGENT_TEMPLATE_NAME(S) are obsolete."
  echo "   Use LOCAL_TEMPLATE_*, ROLE_DATABUS_TEMPLATE_* and ROLE_INFOBUS_TEMPLATE_* (see .env.example)."
  exit 1
fi
# ---------------------------------------------------------------------------
# Wait for the Zabbix API to respond
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Authenticate and obtain the access token
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Try to authenticate with the default credentials
# ---------------------------------------------------------------------------
echo "🔒 Authenticating with default user..."
TOKEN=$(curl -sf -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"user.login\",\"params\":{\"username\":\"$DEFAULT_USER\",\"password\":\"$DEFAULT_PASS\"},\"id\":1}" \
  | grep -o '"result":"[^"]*"' | cut -d'"' -f4)

# ---------------------------------------------------------------------------
# Use the .env credentials if default authentication fails
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Get the configured host group or create it if it does not exist
# ---------------------------------------------------------------------------

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
echo "   - 📁 Group name: '$HOST_GROUP' ID: $GROUP_ID"

# ---------------------------------------------------------------------------
# Import or look up templates based on the host or agent role
# ---------------------------------------------------------------------------
# Keep file/name pairs in the same order. Separate files with commas and names
# with vertical bars.
# Use LOCAL_TEMPLATE_* for the local host and ROLE_*_TEMPLATE_* for each role.
# ---------------------------------------------------------------------------
# Save imported IDs for reuse during this run
# ---------------------------------------------------------------------------
declare -A IMPORTED_IDS=()
_TPL_ID=""
TPL_JSON="[]"

import_template() {
  local _file="$1" _name="$2" _id _req _res _err

  # -------------------------------------------------------------------------
  # Reuse the ID of a template already imported during this run
  # -------------------------------------------------------------------------
  if [ -n "${IMPORTED_IDS[$_file]:-}" ]; then
    _TPL_ID="${IMPORTED_IDS[$_file]}"
    return 0
  fi

  if [ ! -f "$_file" ]; then
    echo -e "❌ ERROR: template file does not exist: ${BLUE}${_file}${NC}"
    exit 1
  fi

  echo -e "📋 Importing/updating: ${ORANGE}${_name}${NC}  (${_file})"

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
      echo "❌ ERROR importing template '${_name}':"
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
      echo "❌ ERROR: could not retrieve the ID for template '${_name}'."
      echo -e "   ${YELLOW}The name in .env must exactly match the 'name:' field in ${BLUE}${_file}${NC}"
      exit 1
  fi

  echo "   - ✅ '${_name}' ready, ID ${_id}"
  IMPORTED_IDS[$_file]="$_id"
  _TPL_ID="$_id"
}
# ---------------------------------------------------------------------------
# Look up an existing Zabbix template without importing a file
# ---------------------------------------------------------------------------
lookup_template() {
  local _name="$1" _id
  echo -e "📋 Existing Zabbix template: ${ORANGE}${_name}${NC}"

  _id=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "$(jq -n --arg n "$_name" \
        '{jsonrpc:"2.0",method:"template.get",params:{filter:{name:[$n]},output:["templateid"]},id:1}')" \
    | jq -r '.result[0].templateid // empty')

  if [ -z "$_id" ]; then
      echo "❌ ERROR: Zabbix does not have a template named '${_name}'."
      echo -e "   ${YELLOW}Copy the exact name from Data collection → Templates${NC}"
      exit 1
  fi

  echo "   - ✅ '${_name}' ready, ID ${_id}"
  _TPL_ID="$_id"
}

# ---------------------------------------------------------------------------
# Resolve file/name pairs and build the JSON array of IDs
# ---------------------------------------------------------------------------
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
    echo "❌ ERROR (${_label}): found ${#_tpl_files[@]} files and ${#_tpl_names[@]} names."
    echo -e "   ${YELLOW}Separate files with commas and names with vertical bars, keeping them in the same order.${NC}"
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
  echo "   - 📋 Templates for ${_label}: $TPL_JSON"
}

# ---------------------------------------------------------------------------
# Resolve the templates to link to the local host
# ---------------------------------------------------------------------------
resolve_templates "${LOCAL_TEMPLATE_FILES:-}" "${LOCAL_TEMPLATE_NAMES:-}" "local host"
LOCAL_TEMPLATE_JSON="$TPL_JSON"

if [ "$LOCAL_TEMPLATE_JSON" = "[]" ]; then
  echo "⚠️  No templates were specified for the local host; using ICMP Ping."
  _icmp=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","method":"template.get","params":{"filter":{"name":["ICMP Ping"]},"output":["templateid"]},"id":1}' \
    | jq -r '.result[0].templateid // empty')
  LOCAL_TEMPLATE_JSON=$(jq -n -c --arg id "$_icmp" '[{templateid: $id}]')
fi

# ---------------------------------------------------------------------------
# Resolve remote-role templates for autoregistration
# ---------------------------------------------------------------------------
resolve_templates "${ROLE_DATABUS_TEMPLATE_FILES:-}" "${ROLE_DATABUS_TEMPLATE_NAMES:-}" "databus role"
DATABUS_TEMPLATE_JSON="$TPL_JSON"
resolve_templates "${ROLE_INFOBUS_TEMPLATE_FILES:-}" "${ROLE_INFOBUS_TEMPLATE_NAMES:-}" "infobus role"
INFOBUS_TEMPLATE_JSON="$TPL_JSON"

# ---------------------------------------------------------------------------
# Find the local agent and create or update its configuration
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Check whether the local host already exists in Zabbix
# ---------------------------------------------------------------------------
HOST_ID=$(curl -sf -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"host.get\",\"params\":{\"filter\":{\"host\":[\"$LOCAL_HOST_NAME\"]}},\"id\":5}" \
  | grep -o '"hostid":"[^"]*"' | head -1 | cut -d'"' -f4)

# ---------------------------------------------------------------------------
# Update the interface and templates if the host already exists
# ---------------------------------------------------------------------------
if [ -n "$HOST_ID" ]; then
    echo "🔄 Host already exists (ID: $HOST_ID), updating interface..."

  # -------------------------------------------------------------------------
  # Get the host's current interface
  # -------------------------------------------------------------------------
  IFACE_ID=$(curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"hostinterface.get\",\"params\":{\"hostids\":\"$HOST_ID\"},\"id\":6}" \
    | grep -o '"interfaceid":"[^"]*"' | head -1 | cut -d'"' -f4)

  # -------------------------------------------------------------------------
  # Verify that an interface was found
  # -------------------------------------------------------------------------
  if [ -z "$IFACE_ID" ]; then
    echo "❌ No interface found for host $HOST_ID, exiting..."
    exit 1
  fi

  # -------------------------------------------------------------------------
  # Update the interface to use the configured DNS name
  # -------------------------------------------------------------------------
  curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"hostinterface.update\",\"params\":{\"interfaceid\":\"$IFACE_ID\",\"type\":1,\"useip\":0,\"dns\":\"$HOST_DNS\",\"ip\":\"\",\"port\":\"$HOST_PORT\",\"main\":1},\"id\":7}" > /dev/null
  echo "   - ✅ Interface updated to DNS: $HOST_DNS:$HOST_PORT"
  # -------------------------------------------------------------------------
  # Apply the desired templates and clear any that have been removed
  # -------------------------------------------------------------------------
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
  echo "   - ✅ Templates relinked: $LOCAL_TEMPLATE_JSON"
  [ "$_clear" != "[]" ] && echo "   - 🧹 Templates unlinked and cleared: $_clear"
else

  # -------------------------------------------------------------------------
  # Create the host with its interface, group, and templates if it does not exist
  # -------------------------------------------------------------------------
  echo "➕ Creating host: $LOCAL_HOST_NAME"
  curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"host.create\",\"params\":{\"host\":\"$LOCAL_HOST_NAME\",\"interfaces\":[{\"type\":1,\"main\":1,\"useip\":0,\"ip\":\"\",\"dns\":\"$HOST_DNS\",\"port\":\"$HOST_PORT\"}],\"groups\":[{\"groupid\":\"$GROUP_ID\"}],\"templates\":$LOCAL_TEMPLATE_JSON},\"id\":8}" > /dev/null
  echo "✅ Host created with DNS: $HOST_DNS:$HOST_PORT"
fi
# ---------------------------------------------------------------------------
# Delete the default "Zabbix server" host
# ---------------------------------------------------------------------------
DEFAULT_HOST_ID=$(curl -sf -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"host.get\",\"params\":{\"filter\":{\"host\":[\"Zabbix server\"]}},\"id\":5}" \
  | grep -o '"hostid":"[^"]*"' | head -1 | cut -d'"' -f4)

curl -sf -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"host.delete\",\"params\":[\"$DEFAULT_HOST_ID\"],\"id\":7}"

# ---------------------------------------------------------------------------
# Prepare separate autoregistration actions for each remote-agent role
# ---------------------------------------------------------------------------
# Require both the common metadata and the role declared in ZBX_METADATA to
# match, so the local host does not match remote-agent actions.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Remove the previous action that applied the same templates to every agent
# ---------------------------------------------------------------------------
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
  echo "   - 🗑️  Previous action 'Self-registration-agents-simovilab' deleted"
fi

# ---------------------------------------------------------------------------
# Create or update the autoregistration action for a role
# ---------------------------------------------------------------------------
upsert_role_action() {
  local _role="$1" _tpl_json="$2" _name="Self-registration-simovilab-$1" _action_id _body

  if [ "$_tpl_json" = "[]" ]; then
    echo "⚠️  Role '${_role}' has no templates in .env; its autoregistration action will not be created."
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
    echo "   - ✅ Action '${_name}' created: $_tpl_json"
  else
    # -----------------------------------------------------------------------
    # Update the action to apply the latest .env changes
    # -----------------------------------------------------------------------
    curl -s -X POST "$API_URL" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $TOKEN" \
      -d "$(jq -n -c --arg id "$_action_id" --argjson b "$_body" '{jsonrpc:"2.0",method:"action.update",params:({actionid:$id}+$b),id:1}')" > /dev/null
    echo "   - 🔄 Action '${_name}' updated: $_tpl_json"
  fi
}

echo "ℹ️  Configuring autoregistration actions for remote agents"
upsert_role_action databus "$DATABUS_TEMPLATE_JSON"
upsert_role_action infobus "$INFOBUS_TEMPLATE_JSON"

# ---------------------------------------------------------------------------
# Configure PSK authentication for autoregistration
# ---------------------------------------------------------------------------
curl -s -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"autoregistration.update\",\"params\":{\"tls_accept\":2,\"tls_psk_identity\":\"$ZBX_TLSPSKIDENTITY\",\"tls_psk\":\"$PSK_VALUE\"},\"id\":1}" > /dev/null
echo "   - 🔑 Autoregistration will use TLS PSK authentication"

# ---------------------------------------------------------------------------
# Create the web user and remove the default account when appropriate
# ---------------------------------------------------------------------------
if [ "$flag_new_user" = "TRUE" ]; then
  echo "ℹ️  Creating new user '$API_WEB_USER' and removing default account"
  echo "🔍 Getting Super Admin role..."
  ROLE_ID=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","method":"role.get","params":{"filter":{"name":["Super admin role"]},"output":["roleid"]},"id":1}' \
    | grep -o '"roleid":"[^"]*"' | head -1 | cut -d'"' -f4)
  
  # -------------------------------------------------------------------------
  # Verify that the super administrator role was found
  # -------------------------------------------------------------------------
  if [ -z "$ROLE_ID" ]; then
    echo "❌ Super Admin role not found, exiting..."
    exit 1
  fi
  echo "   - 👑 Role ID: $ROLE_ID"

  # -------------------------------------------------------------------------
  # Get the Zabbix administrators group ID
  # -------------------------------------------------------------------------
  echo "🔍 Getting 'Zabbix administrators' group ID..."
  USERGROUP_ID_0=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","method":"usergroup.get","params":{"filter":{"name":["Zabbix administrators"]},"output":["usrgrpid"]},"id":1}' \
    | grep -o '"usrgrpid":"[^"]*"' | head -1 | cut -d'"' -f4)

  # -------------------------------------------------------------------------
  # Verify that the administrators group was found
  # -------------------------------------------------------------------------
  if [ -z "$USERGROUP_ID_0" ]; then
    echo "❌ Group 'Zabbix administrators' not found, exiting..."
    exit 1
  fi
  echo "   - 👥 'Zabbix administrators' usergroup ID: $USERGROUP_ID_0"

  # -------------------------------------------------------------------------
  # Get the internal group ID
  # -------------------------------------------------------------------------
  echo "🔍 Getting 'Internal' group ID..."
  USERGROUP_ID_1=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","method":"usergroup.get","params":{"filter":{"name":["Internal"]},"output":["usrgrpid"]},"id":1}' \
    | grep -o '"usrgrpid":"[^"]*"' | head -1 | cut -d'"' -f4)

  # -------------------------------------------------------------------------
  # Verify that the internal group was found
  # -------------------------------------------------------------------------
  if [ -z "$USERGROUP_ID_1" ]; then
    echo "❌ Group 'Internal' not found, exiting..."
    exit 1
  fi
  echo "   - 👥 'Internal' usergroup ID: $USERGROUP_ID_1"

  # -------------------------------------------------------------------------
  # Check whether the configured user already exists
  # -------------------------------------------------------------------------
  NEW_USER_ID=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"user.get\",\"params\":{\"filter\":{\"username\":[\"$API_WEB_USER\"]},\"output\":[\"userid\"]},\"id\":1}" \
    | grep -o '"userid":"[^"]*"' | head -1 | cut -d'"' -f4)

  # -------------------------------------------------------------------------
  # Skip creation if the user already exists
  # -------------------------------------------------------------------------
  if [ -n "$NEW_USER_ID" ]; then
    echo "ℹ️  User '$API_WEB_USER' already exists"
  else
    # -----------------------------------------------------------------------
    # Create the user if it does not already exist
    # -----------------------------------------------------------------------
    echo "➕ Creating user: $API_WEB_USER..."
    NEW_USER_ID=$(curl -s -X POST "$API_URL" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $TOKEN" \
      -d "{\"jsonrpc\":\"2.0\",\"method\":\"user.create\",\"params\":{\"username\":\"$API_WEB_USER\",\"passwd\":\"$API_WEB_PASS\",\"roleid\":\"$ROLE_ID\",\"usrgrps\":[{\"usrgrpid\":\"$USERGROUP_ID_0\"},{\"usrgrpid\":\"$USERGROUP_ID_1\"}],\"name\":\"$API_WEB_USER\",\"autologin\":0,\"autologout\":\"0\"},\"id\":1}" \
      | grep -o '"userids":\["[^"]*"' | cut -d'"' -f4)

    # -----------------------------------------------------------------------
    # Verify that user creation returned the user ID
    # -----------------------------------------------------------------------
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

  # -------------------------------------------------------------------------
  # Authenticate as the new user before removing the default account
  # -------------------------------------------------------------------------
  echo "   - 🔑 Authenticating with new user..."
  NEW_TOKEN=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"user.login\",\"params\":{\"username\":\"$API_WEB_USER\",\"password\":\"$API_WEB_PASS\"},\"id\":1}" \
    | grep -o '"result":"[^"]*"' | cut -d'"' -f4)

  # -------------------------------------------------------------------------
  # Verify that authentication with the new user succeeded
  # -------------------------------------------------------------------------
  if [ -z "$NEW_TOKEN" ]; then
    echo "⚠️  Could not authenticate with new user, exiting..."
    exit 1
  fi

  echo "   - 🔑 New user token obtained successfully"
  echo "ℹ️  Proceeding to delete default account"

  # -------------------------------------------------------------------------
  # Look up the default administrator account
  # -------------------------------------------------------------------------
  echo "🔍 Looking up Admin user..."
  ADMIN_ID=$(curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $NEW_TOKEN" \
    -d '{"jsonrpc":"2.0","method":"user.get","params":{"filter":{"username":["Admin"]},"output":["userid"]},"id":1}' \
    | grep -o '"userid":"[^"]*"' | head -1 | cut -d'"' -f4)

  # -------------------------------------------------------------------------
  # Skip deletion if the default account no longer exists
  # -------------------------------------------------------------------------
  if [ -z "$ADMIN_ID" ]; then
    echo "⚠️  Admin user not found, may have already been deleted, exiting..."
    exit 0
  fi
  echo "   - 🔍 Admin ID: $ADMIN_ID"

  # -------------------------------------------------------------------------
  # Transfer maps from the default account to the new user
  # -------------------------------------------------------------------------
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

  # -------------------------------------------------------------------------
  # Transfer dashboards from the default account to the new user
  # -------------------------------------------------------------------------
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

  # -------------------------------------------------------------------------
  # Delete the default account after transferring its resources
  # -------------------------------------------------------------------------
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