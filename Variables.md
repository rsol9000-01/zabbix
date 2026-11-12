# Variables de Zabbix

Este documento ordena las variables más relevantes del flujo de despliegue y configuración, por capas de visibilidad: primero las que se leen o entregan al script, luego las de estado/runtime, luego las internas de funciones y finalmente las de validación o bucles.

| Capa | Variable | Qué hace | Nueva nomenclatura | Dónde se define / para qué se usa |
| --- | --- | --- | --- | --- |
| 1. Expuesta / entrada | `API_URL` | URL del endpoint JSON-RPC de Zabbix. |  | Definida en el `.env` del repositorio y también reutilizada por `zabbix-conf-en.sh` para autenticarse y ejecutar cada llamada a la API de Zabbix. Es la base de todo el flujo de configuración. |
| 1. Expuesta / entrada | `DEFAULT_USER` / `DEFAULT_PASS` | Credenciales por defecto del administrador de Zabbix. |  | Definidas en el `.env` o en la configuración del despliegue; el script las usa como primer intento de login antes de probar las credenciales del usuario `API_WEB_USER`. |
| 1. Expuesta / entrada | `API_WEB_USER` / `API_WEB_PASS` | Usuario definido en el entorno para fallback si falla el usuario por defecto. |  | Definidas en `.env`. Se usan en `zabbix-conf-en.sh` para un segundo intento de autenticación cuando el admin por defecto ya no es válido. |
| 1. Expuesta / entrada | `HOST_GROUP` | Grupo del host local en Zabbix. |  | Definida en `.env`. El script la usa para crear o localizar el grupo que recibirá el host local y los hosts de agente autoconfigurados. |
| 1. Expuesta / entrada | `LOCAL_HOST_NAME` | Nombre del host local en Zabbix. |  | Definida en `.env` y usada en `host.get`/`host.create` para identificar el host principal de la infraestructura local. |
| 1. Expuesta / entrada | `HOST_DNS` / `HOST_PORT` | DNS/IP y puerto de la interfaz del host local. |  | Definidas en `.env` y usadas para crear la interfaz del host en Zabbix (`hostinterface.update` / `host.create`). Es la dirección que Zabbix usa para contactar al agente. |
| 1. Expuesta / entrada | `LOCAL_TEMPLATE_FILES` / `LOCAL_TEMPLATE_NAMES` | Files y nombres de templates necesarios para el host local. |  | Se leen desde `.env` y se resuelven en `resolve_templates()`. Sirven para importar o enlazar los templates del host local. |
| 1. Expuesta / entrada | `ROLE_DATABUS_TEMPLATE_FILES` / `ROLE_DATABUS_TEMPLATE_NAMES` | Templates que se aplican al rol `databus` durante autoregistro. |  | Definidas en `.env` y usadas en `resolve_templates()` para enviar al template correcto a los agentes que se registren con el metadata `databus`. |
| 1. Expuesta / entrada | `ROLE_INFOBUS_TEMPLATE_FILES` / `ROLE_INFOBUS_TEMPLATE_NAMES` | Templates para el rol `infobus` en la auto-alta de agentes remotos. |  | Definidas en `.env` y usadas en `resolve_templates()` para asociar los templates del modelo infobus a los agentes remotos. |
| 1. Expuesta / entrada | `ZBX_METADATA` | Metadata que identifica cada host/rol en autoregistro. |  | Definida en `.env` y usada por la condición de la acción de autoregistro para distinguir entre host local y agentes remotos. |
| 1. Expuesta / entrada | `ZBX_TLSPSKIDENTITY` / `ZBX_TLSPSKFILENAME` | Identity y archivo de la PSK para TLS. |  | Definidas en `.env`/mounts del contenedor. Se usan para que la auto-registro de agentes cobre un PSK compatible con la configuración del servidor Zabbix. |
| 1. Expuesta / entrada | `PSK_SEED` | Semilla opcional para derivar la PSK. |  | Entrada del script `zabbix-dev.sh`. Se usa para generar la PSK determinista cuando se quiere reutilizar la misma clave entre servidor y agente. |
| 2. Estado de runtime | `flag_new_user` | Indica si el login inicial se hizo con el usuario por defecto y por tanto hay que crear el usuario web nuevo. |  | Variable local del script; se define en `zabbix-conf-en.sh` tras `user.login` y se usa para decidir si se debe crear el usuario externo. |
| 2. Estado de runtime | `PSK_VALUE` | PSK efectiva, leída desde el archivo del contenedor o derivada desde la semilla. |  | Se define en `zabbix-conf-en.sh` y luego se usa en `autoregistration.update` para configurar la autenticación TLS con PSK. |
| 2. Estado de runtime | `TOKEN` | Token de acceso a la API de Zabbix. |  | Se define después del login y se reutiliza para toda la secuencia de `host.get`, `host.create`, `action.create`, `usergroup.get` y similares. |
| 2. Estado de runtime | `GROUP_ID` | `groupid` del host group dado por `HOST_GROUP`. |  | Se calcula en tiempo de ejecución con `hostgroup.get` y se almacena para usarse en `host.create` o `host.update`. |
| 2. Estado de runtime | `HOST_ID` | `hostid` del host local. |  | Se calcula con `host.get` y se usa para actualizar la interfaz y los templates del host ya existente. |
| 2. Estado de runtime | `IFACE_ID` | ID de la interfaz asociada al host. |  | Se obtiene con `hostinterface.get`; luego se actualiza para que Zabbix conozca el DNS/puerto correcto del agente. |
| 2. Estado de runtime | `IMPORTED_IDS` | Mapa de `archivo -> templateid` creado durante la ejecución. |  | Variable interna del script; evita importar una y otra vez el mismo template declarativo durante la misma ejecución. |
| 2. Estado de runtime | `TPL_JSON` | JSON de IDs de templates resueltos para enlazar al host o a la acción. |  | Se construye en `resolve_templates()` y se usa en `host.update`, `host.create` o `action.create`/`action.update`. |
| 2. Estado de runtime | `LOCAL_TEMPLATE_JSON` | JSON definitivo de templates para el host local. |  | Se define en el script como resultado de `resolve_templates` y se usa en la creación o ajuste del host local principal. |
| 2. Estado de runtime | `DATABUS_TEMPLATE_JSON` | JSON definitivo de templates para la acción de autoregistro del rol `databus`. |  | Se calcula con `resolve_templates` y se usa al crear la action de autoregistro para agentes `databus`. |
| 2. Estado de runtime | `INFOBUS_TEMPLATE_JSON` | JSON definitivo de templates para la acción de autoregistro del rol `infobus`. |  | Se calcula con `resolve_templates` y se usa al crear la action de autoregistro para agentes `infobus`. |
| 2. Estado de runtime | `DEFAULT_HOST_ID` | `hostid` del host por defecto `Zabbix server`. |  | Se obtiene con `host.get`; se usa para eliminar el host predeterminado si ya existe y no se desea en la infraestructura. |
| 2. Estado de runtime | `GROUP_ADD_ID` / `GROUP_REMOVE_ID` | IDs del grupo a añadir y quitar en la acción de autoregistro. |  | Se calculan con `hostgroup.get` y luego se incorporan en la action para que el agente se clasifique correctamente al registrarse. |
| 2. Estado de runtime | `LEGACY_ACTION_ID` | ID de la acción antigua de autoregistro que se quiere reemplazar. |  | Se obtiene con `action.get` y se borra para evitar que la acción previa siga aplicando templates incorrectos. |
| 2. Estado de runtime | `ROLE_ID` | `roleid` del rol `Super admin role`. |  | Se obtiene durante la creación del usuario web y se usa para asignar el rol administrativo al usuario de API. |
| 2. Estado de runtime | `USERGROUP_ID_0` / `USERGROUP_ID_1` | IDs de los grupos de usuarios `Zabbix administrators` e `Internal`. |  | Se obtienen con `usergroup.get` y se usan para asociar permisos de administración al usuario que se crea. |
| 3. Interna de función | `_TPL_ID` | ID del último template importado o encontrado en Zabbix. |  | Variable local dentro de `import_template()`; se usa como valor de retorno del proceso de importación. |
| 3. Interna de función | `_file`, `_name`, `_id`, `_req`, `_res`, `_err` | Datos de importación y respuesta del template. |  | Variables locales de `import_template()`. Se usan para validar archivos, armar el payload JSON-RPC y declarar los errores exactos. |
| 3. Interna de función | `_files`, `_names`, `_label`, `_ids`, `_i` | Parámetros que resuelven los templates de la configuración. |  | Variables locales de `resolve_templates()`. Se usan para recorrer los pares `archivo / nombre` y producir `TPL_JSON`. |
| 3. Interna de función | `_tpl_files`, `_tpl_names` | Arrays con archivos y nombres de templates. |  | Se crean dentro de `resolve_templates()` y se rellenan separando los valores por `,` y `|` para mantener la relación entre ambos. |
| 3. Interna de función | `_icmp` | ID del template `ICMP Ping` del sistema. |  | Se usa como fallback cuando no hay templates definidos para el host local; entonces Zabbix usa un template mínimo de ping. |
| 3. Interna de función | `_linked`, `_clear` | Templates actualmente enlazados y los que deben quitarse del host. |  | Variables locales de la rama que actualiza el host existente; se usan para re-sincronizar los templates y limpiar los no deseados. |
| 3. Interna de función | `_role`, `_tpl_json`, `_name`, `_action_id`, `_body` | Datos de la action de autoregistro por rol. |  | Variables locales de `upsert_role_action()`, usadas para generar o actualizar la action que aplica templates a agentes remotos según su rol. |
| 4. Validación / bucles | `MAX_RETRIES`, `COUNT` | Número máximo de reintentos y contador para esperar la API. |  | Definidas en `zabbix-conf-en.sh`. Se usan dentro del `until curl ...` para esperar a que la API de Zabbix responda antes de seguir. |
| 4. Validación / bucles | `trim() -> s` | Cadena con espacios laterales eliminados. |  | Función local `trim()` definida in-script. Sirve para limpiar valores de `LOCAL_TEMPLATE_FILES` y `ROLE_*_TEMPLATE_FILES` sin depender de utilidades externas. |
| 4. Validación / bucles | `VALUE`, `ITEMKEY`, `SCRIPTNAME`, `NEW_LINE`, `EXISTS` | Variables del loop que procesa `AGENT_PARAMS_*` desde `.env`. |  | Se usan en el bloque que recorre cada línea de `.env` y crea `UserParameter` en la configuración del agente. |
| 4. Validación / bucles | `RED`, `GREEN`, `YELLOW`, `BLUE`, `CYAN`, `GRAY`, `NC` | Colores para los mensajes del terminal. |  | Definidas en el script. No afectan la lógica de despliegue; solo mejoran la legibilidad del log. |
| 4. Validación / bucles | `REPO_ROOT`, `COMPOSE_FILE`, `DOCKER_GID`, `FLAG_SERVER` | Variables del entorno general del script. |  | Definidas en `zabbix-dev.sh` y usadas para determinar la ruta del repo, el archivo `docker-compose` correcto y el tipo de despliegue (server o agent). |

## Observación de naming

La convención más clara para renombrar sería:

- prefijo por dominio: `zbx_`, `env_`, `cfg_`, `api_`, `host_`, `tpl_`, `run_`
- evitar nombres cortos ambiguos (`ID`, `COUNT`, `FLAG`) sin contexto
- separar variables de entrada, estado y locales

Ejemplo:

- `API_URL` -> `zbx_api_url`
- `HOST_GROUP` -> `zbx_host_group`
- `TOKEN` -> `zbx_api_token`
- `TPL_JSON` -> `zbx_template_ids_json`
- `MAX_RETRIES` -> `zbx_api_max_retries`
- `PSK_VALUE` -> `zbx_tls_psk_value`
