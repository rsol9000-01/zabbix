# Zabbix Docker Monitoring Stack

Docker Compose deployment for a Zabbix server, PostgreSQL, web frontend, a local Agent2 container, and optional remote Agent2 containers.

## 1. Quick Start

### Prerequisites

- Linux host with Docker Engine and the Docker Compose plugin.
- `curl`, `openssl`, and a `docker` Unix group. The startup script can install `curl` and `openssl` on Debian-based systems when missing; it does not install Docker.
- The user running the script must be able to use Docker and change ownership/permissions on project files.
- For the server deployment, allow the configured web ports and TCP `10051` through the host firewall. Remote active agents must be able to reach the server on TCP `10051`.

### Start the server

```bash
git clone <repository-url>
cd <repository-directory>
cp .env.example .env
```

Edit `.env` before starting. Replace all deployment-specific values and credentials; the checked-in example may contain environment-specific settings. Keep `.env` private and do not commit it.

```bash
./scripts/zabbix-dev.sh server 'ReplaceWithYourSeed.2026'
```

Use your own strong seed, not the example string. The same exact seed is needed when deploying an agent on another host. See [PSK and seed](#psk-and-seed).

The script starts the stack and follows the `zabbix-init` logs. Check container status with:

```bash
docker compose -f docker-compose.yml ps
```

Open `http://<SERVER_IP>:<PORT_HTTP>` or `https://<SERVER_IP>:<PORT_HTTPS>`, using the values configured in `.env`. HTTPS requires the web container's certificate volume to be provisioned; the startup script does not generate web certificates.

## 2. Server How-To

Run the server mode on the Docker host that will run PostgreSQL and Zabbix Server:

```bash
./scripts/zabbix-dev.sh server 'YourSameStrongSeed.2026'
```

The root `docker-compose.yml` starts PostgreSQL, Zabbix Server, the web frontend, a local Agent2, and the one-shot `zabbix-init` service. The script prepares Agent2 configuration and sudoers files, generates the PSK file, and runs Compose. `zabbix-init` then configures Zabbix through its JSON-RPC API.

### Server `.env` values to customize

| Variables | Purpose |
| --- | --- |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | PostgreSQL credentials and database name. Use unique, strong credentials. |
| `ZBX_DB_IP`, `ZBX_SERVER_IP` | Database and server addresses as seen by containers; Compose service names are suitable on the default Compose network. |
| `PORT_HTTP`, `PORT_HTTPS`, `TZ`, `PHP_TZ`, `ZBX_ENABLE_SNMP_TRAPS` | Published web ports, time zones, and SNMP-trap setting. |
| `API_URL` | Zabbix API endpoint reachable from `zabbix-init`, usually the dashboard service URL inside the Compose network. |
| `DEFAULT_USER`, `DEFAULT_PASS` | Initial Zabbix login used during first-time initialization. Change from defaults. |
| `API_WEB_USER`, `API_WEB_PASS` | Administrative account created on first initialization and used on later runs. |
| `SCRIPT_POST_INSTALL` | Relative path to the post-install script, normally `scripts/zabbix-conf-en.sh`. |
| `LOCAL_HOST_NAME`, `HOST_DNS`, `HOST_PORT`, `HOST_GROUP` | Zabbix host name, local Agent2 interface DNS/port, and host group. `HOST_DNS` must resolve from the Zabbix Server container. |
| `AGENT_TEMPLATE_NAME`, `AGENT_TEMPLATE_FILE` | Template name and YAML file to import. Configure both to import/update the supplied template. |
| `ZBX_METADATA` | Metadata string used to match remote agents in the autoregistration action. Use the same value on server and agents. |
| `ZBX_TLSPSKIDENTITY`, `ZBX_TLSPSKFILENAME` | PSK identity and PSK filename. The filename must match the mounted file name, normally `zabbix_agentd.psk`. |
| `ZBX_AGENT_CONFIG_FILE`, `AGENT_SUDOERS_FILE` | Agent2 configuration and sudoers filenames under `agent2/`. |
| `AGENT_PARAMS_<suffix>` | Optional `item-key:script-name` entries used by `zabbix-dev.sh` to add custom Agent2 UserParameters. Example: `AGENT_PARAMS_00=host.tcp.established:zabbix-host-tcp-established.sh`. |

`AGENT_TEMPLATE_NAME` and `AGENT_TEMPLATE_FILE` are both needed for YAML import. If either is empty or the file is unavailable, the post-install script attempts to use Zabbix's `ICMP Ping` template instead.

### What the post-install script does

The script waits for the API (up to 10 attempts, with 5-second intervals), authenticates first with the initial credentials and then falls back to `API_WEB_USER`, and creates the configured host group if missing. It imports/updates the YAML template when configured, creates the local host or updates its interface if the host already exists, and creates an autoregistration action for agents whose metadata matches `ZBX_METADATA`.

Important behavior to account for:

- On first initialization, when the initial account still works, it creates `API_WEB_USER`, transfers maps and dashboards from `Admin`, and deletes the `Admin` account. On later runs it authenticates as the configured web user and skips that account migration.
- If the local host already exists, the script updates its interface only; it does not reconcile its group or assigned template.
- It attempts to delete the host named `Zabbix server` after local host setup.
- The autoregistration action and its TLS PSK settings are created/configured only when that named action does not already exist. If you change metadata, groups, template, or PSK settings later, update the existing action in Zabbix manually or remove it before rerunning initialization.
- The template import uses create-missing and update-existing rules for templates, items, triggers, graphs, discovery rules, and value maps.

View initialization logs with `docker compose -f docker-compose.yml logs -f zabbix-init`. The init container exits after completing its run.

## 3. Agent How-To

Deploy remote Agent2 on the machine to be monitored. Check out this repository there, configure its `.env`, and ensure the host can resolve/reach the server's `SERVER_FQDN` on TCP `10051` for active checks.

The remote Compose file joins an external Docker network named `databus_network`. Create that network on the agent host if it does not already exist:

```bash
docker network create databus_network
```

Then deploy the agent:

```bash
./scripts/zabbix-dev.sh agent 'YourSameStrongSeed.2026'
```

The script selects `agent2/docker-compose-agent2.yml` and starts only `remote-zabbix-agent2`; it does not run server initialization. `DOCKER_GID` is read from the local Docker group. The Agent2 container runs privileged and mounts the Docker socket, so deploy it only on trusted hosts.

### Remote-agent `.env` values to customize

| Variables | Purpose |
| --- | --- |
| `SERVER_FQDN` | DNS name or reachable address of the Zabbix Server for active checks. |
| `REMOTE_AGENT_HOSTNAME` | Agent hostname registered in Zabbix. Keep it unique. |
| `AGENT_FQDN` | DNS name used for the agent interface/host-interface environment. |
| `ZBX_METADATA` | Must exactly match the server metadata used by the autoregistration action. |
| `ZBX_PORTS` | Host-to-container port mapping when passive checks are required; for example, `10050:10050`. |
| `ZBX_ACTIVE_ALLOW`, `ZBX_REFRESH_ACTIVE_CHECKS` | Active-check behavior and refresh interval. |
| `ZBX_TLSCONNECT`, `ZBX_TLSACCEPT` | TLS mode for outgoing and incoming agent connections; PSK mode uses `psk`. |
| `ZBX_TLSPSKIDENTITY` | Must exactly match the identity configured for autoregistration on the server. |
| `ZBX_TLSPSKFILE`, `ZBX_TLSPSKFILENAME` | Container path and host-side filename for the PSK file. Keep the host filename and file mount consistent. |
| `ZBX_AGENT_CONFIG_FILE`, `AGENT_SUDOERS_FILE` | Agent configuration and sudoers files mounted into the container. |
| `TZ` | Agent container time zone. |

The agent host also needs the configured files under `agent2/`, scripts under `scripts/user_scripts/` if used, and `psk/zabbix_agentd.psk`. The agent Compose mount expects the PSK file to exist before startup.

## PSK and seed

The optional second argument to `zabbix-dev.sh` is a seed, not the PSK itself. The script validates a length of 8-64 characters, requires uppercase and lowercase letters, a digit, and at least one of `. * + ? -`, then writes the SHA-256 digest of the seed to `psk/zabbix_agentd.psk`.

**Use exactly the same seed in server mode and agent mode.** The deterministic hash then gives both deployments the same PSK value. Keep the seed private and do not put it in shell history if that is a concern for your environment.

If server mode is run without a seed, it generates a random PSK instead. In that case, agent mode cannot reproduce it: securely copy the server's `psk/zabbix_agentd.psk` to the agent host at the same relative path, and use matching `ZBX_TLSPSKIDENTITY` values. Do not generate an independent random PSK on the agent host.

The script currently sets the PSK file mode to `0644`; review file access on shared hosts and protect the file and backups appropriately. Never publish the PSK or seed.

## Operations and security notes

- `.env` is git-ignored; keep credentials there and replace any example values before deployment.
- The server image uses the mutable `latest` tag, while the Agent2 Dockerfile currently uses `zabbix-agent2:ubuntu-7.4.3`. Pin and test image versions deliberately for reproducible production deployments.
- The Agent2 Compose service uses `privileged: true` and mounts `/var/run/docker.sock`, which grants significant host access.
- Configure firewall rules for only the required ports. The server Compose publishes web ports from `PORT_HTTP`/`PORT_HTTPS` and TCP `10051`; the remote agent publishes the mapping in `ZBX_PORTS` when configured.
- Useful commands:

```bash
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs -f zabbix-server-pgsql
docker compose -f docker-compose.yml down
docker compose -f agent2/docker-compose-agent2.yml ps
docker compose -f agent2/docker-compose-agent2.yml down
```
