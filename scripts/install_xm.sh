#!/data/data/com.termux/files/usr/bin/bash
# Run from a private Termux checkout, not /sdcard. No Android root is needed.
set -euo pipefail
umask 077

action=${1:-install}
case "$action" in install|start|doctor|status|pair|test) ;; *) echo "Usage: bash scripts/install_xm.sh [install|start|doctor|status|pair|test]" >&2; exit 2 ;; esac
if [[ ${PREFIX:-} != /data/data/com.termux/files/usr || $(id -u) == 0 ]]; then
    echo 'Run this script in the standard com.termux app (F-Droid or official GitHub build).' >&2
    exit 1
fi
case $(uname -m) in aarch64|x86_64) ;; *) echo 'DAXQO for XM needs a 64-bit ARM or x86 device.' >&2; exit 1 ;; esac
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
case "$repo" in "$HOME"/*) ;; *) echo 'Keep the source checkout inside private Termux HOME.' >&2; exit 1 ;; esac
container=daxqo-xm
host_uid=$(id -u)
host_gid=$(id -g)
if [[ $action == install || $action == start ]]; then
    termux-wake-lock
    trap 'termux-wake-unlock' EXIT
fi

if [[ $action == install ]]; then
    # Termux is rolling-release: selective upgrades can leave libcurl/OpenSSL
    # ABI mismatches in the APK's old bootstrap. apt works even if curl is broken.
    apt-get update
    apt-get -y -o Dpkg::Options::=--force-confold dist-upgrade
    apt-get install -y proot-distro curl
    # A failed registry pull can leave only these two empty directories.
    # rmdir never removes an existing filesystem or any user data.
    empty_container=$PREFIX/var/lib/proot-distro/containers/$container
    if [[ -d $empty_container/rootfs ]] && rmdir -- "$empty_container/rootfs" 2>/dev/null; then
        rmdir -- "$empty_container" 2>/dev/null || { echo 'Incomplete container metadata needs inspection.' >&2; exit 1; }
    fi
    # Support both the original distro plugins and the new OCI-based v5 CLI.
    if [[ ! -d $PREFIX/var/lib/proot-distro/installed-rootfs/$container && ! -d $PREFIX/var/lib/proot-distro/containers/$container ]]; then
        pd_help=$(proot-distro install --help 2>&1)
        if [[ $pd_help == *'--name'* ]]; then
            # Official Termux Debian image: avoids Docker Hub authentication,
            # which is often unreachable from a mainland China mobile network.
            case $(uname -m) in
                aarch64) digest=3834a11cbc6496935760bdc20cca7e2c25724d0cd8f5e4926da8fd5ca1857918 ;;
                x86_64) digest=4b8f33b80a10d734ff935e5934588572f860c0c38a68bf91db59af0580370716 ;;
            esac
            rootfs=${DAXQO_XM_ROOTFS:-$HOME/.cache/daxqo-xm/debian-trixie-$(uname -m)-pd-v4.29.0.tar.xz}
            if [[ ! -f $rootfs ]]; then
                [[ -z ${DAXQO_XM_ROOTFS:-} ]] || { echo 'Specified rootfs is missing.' >&2; exit 1; }
                mkdir -p -- "$(dirname -- "$rootfs")"
                curl --fail --location --proto '=https' --proto-redir '=https' \
                    --connect-timeout 15 --max-time 300 --retry 2 \
                    "https://github.com/termux/proot-distro/releases/download/v4.29.0/debian-trixie-$(uname -m)-pd-v4.29.0.tar.xz" \
                    --output "$rootfs.part"
                mv -- "$rootfs.part" "$rootfs"
            fi
            actual=$(sha256sum -- "$rootfs")
            [[ ${actual%% *} == "$digest" ]] || { echo 'Official Debian rootfs SHA256 mismatch.' >&2; exit 1; }
            proot-distro install "$rootfs" --name "$container"
        else
            proot-distro install --override-alias "$container" debian
        fi
    fi
    proot-distro login "$container" --bind "$repo:/opt/daxqo-src" -- \
        /bin/bash /opt/daxqo-src/scripts/xm_guest.sh bootstrap "$host_uid" "$host_gid" "${DAXQO_XM_DNS:-223.5.5.5 223.6.6.6}"
elif ! command -v proot-distro >/dev/null; then
    echo 'Run the install action first.' >&2
    exit 1
fi

if [[ $action == start ]]; then
    # Keep this session open: PRoot is the parent of all guest services.
    proot-distro login "$container" --user daxqo --bind "$repo:/opt/daxqo-src" -- \
        /bin/bash /opt/daxqo-src/scripts/xm_guest.sh start
else
    proot-distro login "$container" --user daxqo --bind "$repo:/opt/daxqo-src" -- \
        /bin/bash /opt/daxqo-src/scripts/xm_guest.sh "$action"
fi
if [[ $action == install ]]; then
    launcher=$PREFIX/bin/daxqo-xm
    [[ ! -L $launcher ]] || { echo 'Refusing a symlink at the launcher path.' >&2; exit 1; }
    printf '#!%s/bin/bash\nif (( $# == 0 )); then set -- start; fi\nexec %q %q "$@"\n' \
        "$PREFIX" "$PREFIX/bin/bash" "$repo/scripts/install_xm.sh" > "$launcher"
    chmod 700 "$launcher"
    echo 'Ready to start: daxqo-xm start'
fi
