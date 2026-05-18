ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Runtime deps:
#   - python3 / py3-pip    : the worker is now Python
#   - bash / jq            : tiny launcher script and option dumping
#   - tini                 : PID-1 signal handling
#   - build-base + headers : aioesphomeapi pulls noiseprotocol which needs cffi build on some arches
RUN apk add --no-cache \
        bash \
        coreutils \
        jq \
        python3 \
        py3-pip \
        tini \
        build-base \
        python3-dev \
        libffi-dev \
        openssl-dev \
    && pip3 install --break-system-packages \
        "aioesphomeapi>=24.0.0" \
        "zeroconf>=0.131.0" \
    && apk del build-base python3-dev libffi-dev openssl-dev \
    && rm -rf /root/.cache /tmp/*

COPY main.py /opt/main.py
COPY run.sh  /run.sh
RUN chmod +x /run.sh

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/run.sh"]
