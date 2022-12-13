ARG BASE_IMAGE

FROM ${BASE_IMAGE} AS base
FROM ghcr.io/by275/base:ubuntu AS prebuilt

# 
# BUILD
# 
FROM base AS builder

# add go-cron watcher
COPY --from=prebuilt /go/bin/ /bar/usr/local/bin/

# add local files
COPY root/ /bar/

ADD https://raw.githubusercontent.com/by275/docker-base/main/_/etc/cont-init.d/install-pkg /bar/etc/s6-overlay/s6-rc.d/init-install-pkg/run
ADD https://raw.githubusercontent.com/by275/docker-base/main/_/etc/cont-init.d/wait-for-mnt /bar/etc/s6-overlay/s6-rc.d/init-wait-for-mnt/run

RUN \
    echo "**** permissions ****" && \
    chmod a+x \
        /bar/usr/local/bin/* \
        /bar/etc/s6-overlay/s6-rc.d/*/run \
    && \
    echo "**** s6: add services to user/contents.d ****" && \
    mkdir -p /tmp/app/contents.d && \
    for dir in /bar/etc/s6-overlay/s6-rc.d/*; do touch "/tmp/app/contents.d/$(basename "$dir")"; done && \
    mv /tmp/app /bar/etc/s6-overlay/s6-rc.d/user

# 
# RELEASE
# 
FROM $BASE_IMAGE
LABEL maintainer="by275"
LABEL org.opencontainers.image.source https://github.com/by275/docker-plex

ARG DEBIAN_FRONTEND="noninteractive"
ARG APT_MIRROR="archive.ubuntu.com"

ENV \
    PLEX_AUTOSCAN_COLORLOG=1 \
    PLEX_AUTOSCAN_CONFIG="/config/autoscan/config.json" \
    PLEX_AUTOSCAN_QUEUEFILE="/config/autoscan/queue.db" \
    PLEX_AUTOSCAN_CACHEFILE="/config/autoscan/cache.db" \
    PLEX_AUTOSCAN_GIT="https://github.com/by275/plex_autoscan.git" \
    PLEX_AUTOSCAN_VERSION_DOCKER=v0.2.1 \
    PLEX_AUTOSCAN_VERSION=docker

# install packages
RUN \
    echo "**** apt source change for local build ****" && \
    sed -i "s/archive.ubuntu.com/$APT_MIRROR/g" /etc/apt/sources.list && \
    echo "**** install runtime packages ****" && \
    apt-get update && \
    apt-get install -yq --no-install-recommends \
        gcc \
        git \
        jq \
        python3-dev \
        python3-venv \
        sqlite3 \
        && \
    echo "**** install plex_autoscan ****" && \
    git -C /opt clone --depth 1 -b "$PLEX_AUTOSCAN_VERSION_DOCKER" \
        "$PLEX_AUTOSCAN_GIT" && \
    python3 -m venv /usr/pas && \
    /usr/pas/bin/python -m pip install wheel && \
    /usr/pas/bin/python -m pip install -r /opt/plex_autoscan/requirements.txt && \
    echo "**** modify existing s6-rc.d ****" && \
    mv /etc/s6-overlay/s6-rc.d/init-config-end/dependencies.d/init-plex-update \
        /etc/s6-overlay/s6-rc.d/init-config-end/dependencies.d/init-autoscan && \
    echo "**** cleanup ****" && \
    apt-get purge -y \
        gcc \
        python3-dev \
        && \
    apt-get clean autoclean && \
    apt-get autoremove -y && \
    rm -rf \
        /root/.cache \
        /tmp/* \
        /var/tmp/* \
        /var/cache/* \
        /var/lib/apt/lists/*

# add build artifacts
COPY --from=builder /bar/ /

# environment settings
ENV \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    DATE_FORMAT="+%4Y/%m/%d %H:%M:%S" \
    PATCH_LOCAL_MEDIA_BUNDLE=true

HEALTHCHECK --interval=5s --timeout=2s --retries=20 \
    CMD /usr/local/bin/healthcheck || exit 1
