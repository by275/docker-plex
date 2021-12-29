ARG BASE_IMAGE

FROM golang:1.16-bullseye AS builder

ARG GO_CRON_VERSION=0.0.4
ARG GO_CRON_SHA256=6c8ac52637150e9c7ee88f43e29e158e96470a3aaa3fcf47fd33771a8a76d959

RUN mkdir -p /bar

ENV GOBIN=/bar/usr/local/bin

RUN \
  echo "**** build go-cron v${GO_CRON_VERSION} ****" && \
  curl -sL -o go-cron.tar.gz https://github.com/djmaze/go-cron/archive/v${GO_CRON_VERSION}.tar.gz && \
  echo "${GO_CRON_SHA256}  go-cron.tar.gz" | sha256sum -c - && \
  tar xzf go-cron.tar.gz && \
  cd go-cron-${GO_CRON_VERSION} && \
  go install

ARG WATCHER_VERSION=1.0.7

RUN \
  echo "**** build watcher v${WATCHER_VERSION} ****" && \
  go install github.com/radovskyb/watcher/cmd/watcher@v${WATCHER_VERSION}

# add local files
COPY root/ /bar/

ADD https://raw.githubusercontent.com/by275/docker-scripts/master/root/etc/cont-init.d/20-install-pkg /bar/etc/cont-init.d/72-install-pkg
ADD https://raw.githubusercontent.com/by275/docker-scripts/master/root/etc/cont-init.d/30-wait-for-mnt /bar/etc/cont-init.d/73-wait-for-mnt
ADD https://raw.githubusercontent.com/by275/docker-scripts/master/root/etc/cont-init.d/90-custom-folders /bar/etc/cont-init.d/90-custom-folders
ADD https://raw.githubusercontent.com/by275/docker-scripts/master/root/etc/cont-init.d/99-custom-scripts /bar/etc/cont-init.d/99-custom-scripts


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
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    DATE_FORMAT="+%4Y/%m/%d %H:%M:%S"

HEALTHCHECK --interval=5s --timeout=2s --retries=20 CMD healthcheck.sh || exit 1
