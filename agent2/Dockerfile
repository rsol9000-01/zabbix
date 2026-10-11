FROM zabbix/zabbix-agent2:ubuntu-7.4.3
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    iproute2 \
    python3 \
    python3-pip && \
    # Instalar requests omitiendo la restricción de entorno externo de Ubuntu
    pip3 install --no-cache-dir --break-system-packages \
    requests \
    gtfs-realtime-bindings && \
    # Limpieza de temporales para reducir el tamaño de la imagen
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

#FROM zabbix/zabbix-agent2:ubuntu-7.4.3
#USER root
#RUN apt-get update && \
#    apt-get install -y --no-install-recommends iproute2 python3 requests && \
#    rm -rf /var/lib/apt/lists/*