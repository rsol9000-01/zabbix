# 📊 **Zabbix Monitoring Stack – Docker Compose**

![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker)
![Zabbix](https://img.shields.io/badge/Zabbix-7.4.6-D50000?style=for-the-badge)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17.6-336791?style=for-the-badge&logo=postgresql)

Comprehensive monitoring platform based on **Zabbix 7.4** using **Docker Compose** for production-ready deployments.

This stack provides a modular and containerized monitoring environment integrating PostgreSQL, Zabbix Server, Zabbix Web, SNMP Traps, and Zabbix Agent2 into a unified infrastructure monitoring solution.

---

# 🔍 **Overview**

Zabbix Docker Stack is a production-oriented deployment designed for infrastructure supervision, service monitoring, SNMP trap collection, and centralized observability.

The environment is optimized for scalability, persistence, maintainability, and rapid deployment in both educational and enterprise environments.

## 🚀 Key Capabilities

- 📈 Infrastructure Monitoring
- 📡 SNMP Trap Reception & Processing
- 🐳 Fully Containerized Deployment
- 🔒 HTTPS Ready Architecture
- 🖥️ Agent2 Host Monitoring
- 💾 Persistent Storage Support
- 🛠️ External Scripts & Alert Integrations
- 📊 Production-Oriented Modular Design

---

# ✨ **Features**

## 🏭 Production Infrastructure

- Docker Compose based deployment
- Persistent Docker volumes
- Modular service isolation
- HTTPS support with SSL/TLS certificates
- Backup-ready architecture
- Scalable and maintainable environment

## 📡 Monitoring & Data Collection

- Zabbix Server 7.4 LTS
- SNMP trap collection and processing
- Active and passive agent monitoring
- PostgreSQL optimized backend
- External monitoring scripts
- Docker-aware Agent2 monitoring

## 🖥️ Web Interface & Access

- Nginx + PHP-FPM frontend
- HTTP and HTTPS support
- Persistent dashboard configuration
- Production-ready web environment

---

# 🛠️ **Technology Stack**

## 🔋 Backend & Monitoring

- **Zabbix Server 7.4.6**
- **Zabbix Agent2 7.4.3**
- **SNMP Trap Receiver**
- **PostgreSQL 17.6**

## 📊 Data & Storage

- Docker Volumes
- Bind Mounts
- PostgreSQL Storage Engine

## 🚪 Infrastructure & Deployment

- Docker Engine 20.10+
- Docker Compose 2+
- Nginx
- PHP-FPM

## 🔒 Security & Reliability

- HTTPS Support
- Persistent Data Protection
- Container Isolation
- Firewall-ready Architecture

---

# 📦 **Stack Architecture**

| Component             | Version | Description                             |
| --------------------- | ------- | --------------------------------------- |
| **PostgreSQL**        | 17.6    | Primary Zabbix database                 |
| **Zabbix Server**     | 7.4.6   | Central monitoring engine               |
| **Zabbix Web**        | 7.4.6   | Web frontend (Nginx + PHP-FPM)          |
| **Zabbix Agent2**     | 7.4.3   | Advanced monitoring agent for the host  |

---

<img src="./diagramas/zabbix-docker.png" alt="Zabbix Docker Diagram" width="80%">

---

# 🔌 **Port Mapping**

| Port      | Protocol | Purpose                  |
| ---------- | -------- | ------------------------ |
| **8080**   | TCP      | Web Interface (HTTP)     |
| **4443**   | TCP      | Web Interface (HTTPS)    |
| **10051**  | TCP      | Server ↔ Active Agents   |
| **162**    | UDP      | SNMP Trap Reception      |

---

# 💾 **Persistent Volumes**

| Volume                    | Container       | Purpose                        |
| ------------------------- | --------------- | ------------------------------ |
| `zabbix-postgresdb`       | PostgreSQL      | Zabbix database persistence    |
| `zabbix-server`           | Zabbix Server   | Runtime and configuration      |
| `zabbix-snmptraps`        | Zabbix Server   | SNMP trap storage              |
| `zabbix_dashboard_config` | Zabbix Web      | Frontend configuration         |
| `zabbix_certificados`     | Zabbix Web      | SSL/TLS certificates           |

## All volumes persist automatically between container restarts.

---

# 💾 **Bind Mounts**

| Bind Mount                          | Container         | Purpose                          |
| ----------------------------------- | ----------------- | -------------------------------- |
| `/backups`                          | PostgreSQL        | Database backups                 |
| `/usr/lib/zabbix/externalscripts`   | Zabbix Server     | External scripts                 |
| `/usr/lib/zabbix/alertscripts`      | Zabbix Server     | Alert scripts                    |
| `/usr/share/snmp/mibs`              | Zabbix SNMPTraps  | External MIB files               |
| `/var/lib/zabbix/snmptrapd_config`  | Zabbix SNMPTraps  | SNMP trap configuration          |
| `/sys`                              | Zabbix Agent2     | Host monitoring access           |
| `/proc`                             | Zabbix Agent2     | Host monitoring access           |
| `/var/run/docker.sock`              | Zabbix Agent2     | Docker monitoring access         |

---

# 🚀 **Quick Start**

## **1. Prerequisites**

- Docker Engine 20.10+
- Docker Compose 2+
- Linux host recommended
- Available ports:
  - 8080/TCP
  - 4443/TCP
  - 10051/TCP
  - 162/UDP

---

## **2. Clone the Repository**

```bash
git clone https://github.com/rsol9000/zabbix-docker.git
cd zabbix-docker

Alternative download:

wget https://github.com/rsol9000/zabbix-docker/archive/main.zip -O zabbix-stack.zip
3. Configure Environment Variables
cp .env.pub .env
nano .env
Critical Variables
POSTGRES_USER=zabbix_admin
POSTGRES_PASSWORD=super_secure_password
Optional Variables
ZBX_SERVER_HOST=zabbix-server
ZBX_SERVER_NAME=Zabbix Monitoring
ZBX_TIMEZONE=America/Costa_Rica
▶️ Deployment
docker compose up -d

Check service status:

docker compose ps

View logs:

docker compose logs -f
🌐 Web Access
Protocol	URL
HTTP	http://<SERVER_IP>:8080
HTTPS	https://<SERVER_IP>:4443
🔐 Enable HTTPS
cp ssl/cert.pem ssl/key.pem ./certificates/
docker compose restart zabbix-web

Supported certificate sources:

Let’s Encrypt
ACME
Self-signed certificates
Enterprise PKI
🖥️ Zabbix Agents

The stack includes a local Zabbix Agent2 container for monitoring the Docker host.

Example remote agent configuration:

Server=<SERVER_IP>
ServerActive=<SERVER_IP>

Compatible with:

Linux Servers
Virtual Machines
Docker Hosts
Network Appliances
📡 SNMP Trap Monitoring

The receiver listens on UDP port 162.

Example Cisco configuration:

snmp-server host <ZABBIX_SERVER_IP> traps version 2c public
🛠️ Common Administration Commands
# Running containers
docker compose ps

# View logs
docker compose logs -f

# Restart services
docker compose restart zabbix-server

# Stop the environment
docker compose down

# Database backup
docker compose exec postgres pg_dump -U $POSTGRES_USER zabbix > backup.sql
🏗️ Architecture
📊 Service Architecture
🌐 Users → 🖥️ Zabbix Web (Nginx/PHP-FPM)
                      ↓
               📡 Zabbix Server
                      ↓
               💾 PostgreSQL
                      ↓
           📡 SNMP Traps / Agent2
📂 Project Structure
zabbix-docker/
├── 📁 postgres/                 # PostgreSQL configuration
├── 📁 zabbix-server/            # Zabbix server configuration
├── 📁 zabbix-web/               # Frontend and SSL configuration
├── 📁 snmptraps/                # SNMP trap receiver configuration
├── 📁 agent2/                   # Zabbix Agent2 configuration
├── 📁 backups/                  # Database backups
├── 📄 docker-compose.yml        # Main Docker Compose stack
├── 📄 .env.pub                  # Public environment template
└── 📄 README.md                 # Project documentation
🔒 Security Recommendations
Change all default credentials
Restrict exposed ports using firewall rules
Enable HTTPS in production environments
Configure automated backups
Monitor access and trap logs regularly
Keep Docker images updated
Protect Docker socket access
🏭 Deployment Scenarios
Supported Environments
☁️ Cloud VPS Deployments
🖥️ Local Datacenter Infrastructure
🐳 Docker-based Laboratories
🎓 Educational Environments
🏢 Enterprise Monitoring Platforms
📝 Version Notes
Zabbix 7.4.6 — Stable LTS release
PostgreSQL 17.6 — Optimized for intensive monitoring workloads
Agent2 — Enhanced plugin and container monitoring capabilities
💫 Support & Troubleshooting
View Container Logs
docker compose logs -f
Check Running Services
docker compose ps
Restart Specific Service
docker compose restart <service>

Universidad | Academic Project | Docker Monitoring Infrastructure