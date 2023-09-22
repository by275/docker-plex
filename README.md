# docker-plex

Docker image for running Plex Media Server with some teaks

## Tags

`ghcr.io/by275/plex:{baseimage}`

* `baseimage`: `lsio` for [LinuxServer](https://github.com/linuxserver/docker-plex) or `pms` for [plexinc](https://github.com/plexinc/pms-docker)

As of 2022/11/08, no more updates for `pms` tag due to the large difference between two variants.

For a specific version, use `ghcr.io/by275/plex:{baseimage}-{vertag}`

* `vertag`: full version string, e.g. `1.25.6.5577-c8bd13540`

A new image will be re-built and pushed every 6 hours if its base has any changes.

## Customized cont-init.d

### Installing APT or PIP packages

[Source](https://raw.githubusercontent.com/by275/docker-base/main/_/etc/cont-init.d/install-pkg)

 ENV  | Description  | Default  |
|---|---|---|
| `APT_MIRROR`  | if you want to change apt repository |  |
| `INSTALL_APT_PKGS`  | run `apt-get install -yqq --no-install-recommends ${INSTALL_APT_PKGS}` silently for you |  |
| `INSTALL_PIP_PKGS`  | run `python3 -m pip -q install -U ${INSTALL_PIP_PKGS}` for you |  |

### Waiting for mounts, dirs, or files

Sleep 30s until desired mounts, dirs, or files are found.

[Source](https://raw.githubusercontent.com/by275/docker-base/main/_/etc/cont-init.d/wait-for-mnt)

 ENV  | Description  | Default  |
|---|---|---|
| `WAIT_RCLONE_MNTS`  | a `\|`-separated list of mount points for checking fuse.rclone mounts |  |
| `WAIT_MFS_MNTS`  | a `\|`-separated list of mount points for checking fuse.mergerfs mounts |  |
| `WAIT_ANCHOR_DIRS`  | a `\|`-separated list of paths for checking existence of dirs |  |
| `WAIT_ANCHOR_FILES`  | a `\|`-separated list of paths for checking existence of files |  |

### Installing Plex Agents from github repo

 ENV  | Description  | Default  |
|---|---|---|
| `MORE_BUNDLES`  | a space-separated list of `{owner}/{repo}` containig source, e.g. `ThePornDatabase/ThePornDB.bundle` |  |

### Patching LocalMedia.bundle

This patch prevents for PMS from reading mp4 metadata, which is unnecessary and useful if you are running PMS on cloud storage.

ENV  | Description  | Default  |
|---|---|---|
| `PATCH_LOCAL_MEDIA_BUNDLE`  | set something other than `true` to disable | `true` |

## `/usr/local/bin/`

### `cleanup-ptc`

to cleaning up PhotoTranscoder directory `/config/Library/Application Support/Plex Media Server/Cache/PhotoTranscoder` for recovering disk space based on policy.

ENV  | Description  | Default  |
|---|---|---|
| `CLEANUP_PTC_CRON`  | cron for scheduling jobs |  |
| `CLEANUP_PTC_AFTER_DAYS`  | delete files older than this |  |
| `CLEANUP_PTC_EXCEED_GB`  | trigger deleting files if the size of PhotoTranscoder directory is larger than this |  |
| `CLEANUP_PTC_FREEUP_GB`  | how much you want to free? |  |

### `plex`

useful plex-related operation with following subcommands:

* `analyze`: Find metadata items of missing analyzation info and run analyze for you
* `repair`: [Repair a Corrupted Database](https://support.plex.tv/articles/repair-a-corrupted-database/)
* `stats`: Print library status
* `optimize`: Trigger database optimization
* `claim`: Plex Claim

## plex_autoscan

`https://github.com/by275/plex_autoscan` is installed to `/opt/plex_autoscan` and svc-autoscan will be activated if `/config/autoscan/config.json` file exists.

ENV  | Description  | Default  |
|---|---|---|
| `PLEX_AUTOSCAN_VERSION`  | to pin version. possible chocies include `latest` or git branch/tag/hash | `docker` |

## watcher

[watcher](https://github.com/radovskyb/watcher) is for monitoring file system changes.

An example of usage:

rclone mount (update change in remote by polling or manual vfs/refresh ) -> watcher (detect filesystem changes and make an API request for the event) -> plex autoscan (execute PMS scanner for you)

ENV  | Description  | Default  |
|---|---|---|
| `WATCHER_DIRS`  | a `\|`-separated list of dir paths for watching |  |
| `WATCHER_INTERVAL`  |  | `60s` |
| `WATCHER_DOTFILES`  |  | `false` |
