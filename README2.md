📊 Zabbix Docker Stack

Docker Zabbix PostgreSQL Monitoring Infrastructure

Comprehensive monitoring platform based on Zabbix 7.4 using Docker Compose for production-ready deployments.

Zabbix Docker Stack is a modular and containerized monitoring solution designed for infrastructure supervision, SNMP monitoring, trap collection, and centralized observability. The platform integrates PostgreSQL, Zabbix Server, Zabbix Web, SNMP Traps, and Zabbix Agent2 into a unified deployment architecture.
🔍 Overview

This project provides a production-oriented deployment of Zabbix using Docker Compose, optimized for scalability, persistence, and modular management. The environment is designed for educational and enterprise scenarios where rapid deployment, maintainability, and monitoring visibility are required.

The stack includes database persistence, HTTPS-ready frontend configuration, SNMP trap reception, external script integration, and local host monitoring using Agent2.

Key Capabilities
📈 Infrastructure Monitoring: Centralized monitoring for servers, services, and network devices
📡 SNMP Trap Collection: Real-time SNMP trap reception and processing
🐳 Containerized Deployment: Fully Dockerized architecture with persistent storage
🔒 HTTPS Ready: SSL/TLS support for secure web access
🖥️ Agent2 Integration: Advanced host monitoring with container-aware plugins
💾 Persistent Data Storage: Database and configuration persistence through Docker volumes
🛠️ External Script Support: Custom scripts and alert integrations
📊 Production-Oriented Architecture: Modular and maintainable deployment structure
✨ Features
🏭 Production Infrastructure
Containerized Deployment: Docker Compose based deployment
Persistent Storage: Named Docker volumes for critical services
HTTPS Support: SSL/TLS frontend integration
Modular Services: Independent and scalable containers
Service Isolation: Dedicated runtime environments for each component
Backup Ready: Database backup integration through bind mounts
📡 Monitoring & Data Collection
Zabbix Server 7.4 LTS: Central monitoring engine
SNMP Monitoring: Trap collection and processing
Zabbix Agent2: Enhanced monitoring plugins and Docker integration
PostgreSQL Backend: Optimized relational database storage
External Scripts: Custom monitoring and automation support
Active Agent Communication: Support for active and passive checks
🖥️ Web Interface & Access
Nginx-based Web Frontend
HTTP and HTTPS Access
Persistent Dashboard Configuration
Secure SSL Certificate Support
Production-ready PHP-FPM Environment
🛠️ Technology Stack
🔋 Backend & Monitoring
Zabbix Server 7.4.6: Enterprise monitoring platform
Zabbix Agent2 7.4.3: Advanced monitoring agent
SNMP Trap Receiver: Real-time trap processing
PostgreSQL 17.6: Relational database backend
📊 Data & Storage
Docker Volumes: Persistent container storage
Bind Mounts: Host integration for scripts and backups
PostgreSQL Storage Engine: High-performance monitoring database
🚪 Infrastructure & Deployment
Docker Engine 20.10+
Docker Compose 2+
Nginx + PHP-FPM
Linux-based container environment
🔒 Security & Reliability
HTTPS Support
Container Isolation
Persistent Data Protection
Firewall-ready Architecture
Backup Integration
🚀 Quick Start
Prerequisites
Docker Engine 20.10+
Docker Compose 2.0+
Linux host recommended
Available ports:
8080/TCP
4443/TCP
10051/TCP
162/UDP
🛠️ Deployment Setup
1. Clone the Repository
git clone https://github.com/rsol9000/zabbix-docker.git
cd zabbix-docker

Alternative download:

wget https://github.com/rsol9000/zabbix-docker/archive/main.zip -O zabbix-stack.zip
2. Configure Environment Variables
cp .env.pub .env
nano .env
Critical Variables
POSTGRES_USER=zabbix_admin
POSTGRES_PASSWORD=super_secure_password
Optional Variables
ZBX_SERVER_HOST=zabbix-server
ZBX_SERVER_NAME=Zabbix Monitoring
ZBX_TIMEZONE=America/Costa_Rica
3. Start the Environment
docker compose up -d

This command will:

📦 Deploy PostgreSQL
⚙️ Start Zabbix Server
🌐 Launch Zabbix Web Interface
📡 Enable SNMP Trap Receiver
🖥️ Start local Zabbix Agent2
💾 Mount persistent volumes automatically
4. Verify Services
docker compose ps

View logs:

docker compose logs -f
🌐 Access the Platform
Web Interface
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
📡 SNMP Trap Monitoring

The stack includes SNMP trap reception on UDP port 162.

Example Cisco configuration:

snmp-server host <ZABBIX_SERVER_IP> traps version 2c public
🖥️ Zabbix Agents

The environment includes a local Zabbix Agent2 container for monitoring the Docker host.

Example remote agent configuration:

Server=<SERVER_IP>
ServerActive=<SERVER_IP>

Compatible with:

Linux Servers
Virtual Machines
Docker Hosts
Network Appliances
📝 Common Commands
# View running containers
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

💾 Persistent Storage
Docker Volumes
Volume	Service	Purpose
zabbix-postgresdb	PostgreSQL	Database persistence
zabbix-server	Zabbix Server	Runtime and configuration
zabbix-snmptraps	SNMP Traps	Trap storage
zabbix_dashboard_config	Zabbix Web	Frontend configuration
zabbix_certificados	Zabbix Web	SSL/TLS certificates

All volumes persist automatically between container restarts.

📂 Bind Mounts
Mount Path	Service	Purpose
/backups	PostgreSQL	Database backups
/usr/lib/zabbix/externalscripts	Zabbix Server	External scripts
/usr/lib/zabbix/alertscripts	Zabbix Server	Alert scripts
/usr/share/snmp/mibs	SNMP Traps	External MIB files
/var/lib/zabbix/snmptrapd_config	SNMP Traps	SNMP trap configuration
/sys	Agent2	Host monitoring
/proc	Agent2	Host monitoring
/var/run/docker.sock	Agent2	Docker monitoring
🛠️ Project Structure
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
📝 Version Information
Component	Version
Zabbix Server	7.4.6
Zabbix Web	7.4.6
Zabbix Agent2	7.4.3
PostgreSQL	17.6
💫 Support & Maintenance
Troubleshooting
View Container Logs
docker compose logs -f
Check Running Services
docker compose ps
Restart Specific Service
docker compose restart <service>

Universidad | Proyecto Académico | Docker Monitoring Infrastructure