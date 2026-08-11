FROM zabbix/zabbix-agent2:ubuntu-7.4.3
RUN apt-get update && \
    apt-get install -y --no-install-recommends iproute2 && \
    rm -rf /var/lib/apt/lists/*