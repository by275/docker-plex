ARG BASE_IMAGE

FROM ${BASE_IMAGE} AS base
FROM ghcr.io/by275/prebuilt:ubuntu20.04 AS prebuilt

# 
# BUILD
# 
FROM base AS builder

# add go-cron watcher
COPY --from=prebuilt /go/bin/ /bar/usr/local/bin/

# add local files
COPY root/ /bar/

ADD https://raw.githubusercontent.com/by275/docker-scripts/master/root/etc/cont-init.d/20-install-pkg /bar/etc/cont-init.d/72-install-pkg
ADD https://raw.githubusercontent.com/by275/docker-scripts/master/root/etc/cont-init.d/30-wait-for-mnt /bar/etc/cont-init.d/73-wait-for-mnt
ADD https://raw.githubusercontent.com/by275/docker-scripts/master/root/etc/cont-init.d/90-custom-folders /bar/etc/cont-init.d/90-custom-folders
ADD https://raw.githubusercontent.com/by275/docker-scripts/master/root/etc/cont-init.d/99-custom-scripts /bar/etc/cont-init.d/99-custom-scripts

# 
# RELEASE
# 
FROM $BASE_IMAGE
LABEL maintainer="by275"
LABEL org.opencontainers.image.source https://github.com/by275/docker-plex

ARG DEBIAN_FRONTEND="noninteractive"
ARG APT_MIRROR="archive.ubuntu.com"

# add build artifacts
COPY --from=builder /bar/ /

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
        sqlite3 && \
    echo "**** install plex_autoscan ****" && \
    git -C /opt clone --depth 1 \
        https://github.com/by275/plex_autoscan.git && \
    python3 -m venv /opt/plex_autoscan/venv && \
    /opt/plex_autoscan/venv/bin/python -m pip install wheel && \
    /opt/plex_autoscan/venv/bin/python -m pip install -r /opt/plex_autoscan/requirements.txt && \
    echo "**** permissions ****" && \
    chmod a+x /usr/local/bin/* && \
    echo "**** cleanup ****" && \
    apt-get purge -y \
        gcc \
        python3-dev && \
    apt-get clean autoclean && \
    apt-get autoremove -y && \
    rm -rf /tmp/* /var/lib/{apt,dpkg,cache,log}/

# environment settings
ENV \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    DATE_FORMAT="+%4Y/%m/%d %H:%M:%S" \
    PATCH_LOCAL_MEDIA_BUNDLE=true

HEALTHCHECK --interval=5s --timeout=2s --retries=20 \
    CMD /usr/local/bin/healthcheck || exit 1
