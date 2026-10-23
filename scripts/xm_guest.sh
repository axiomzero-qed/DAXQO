#!/bin/bash
set -euo pipefail
umask 077
action=${1:?action required}
repo=/opt/daxqo-src
if [[ $action == bootstrap ]]; then
    uid=${2:?Termux UID required}
    gid=${3:?Termux GID required}
    [[ $uid =~ ^[0-9]+$ && $gid =~ ^[0-9]+$ && $uid -ge 10000 && $gid -ge 10000 ]]
    # glibc does not use Android's resolver. The official rootfs defaults to
    # Google DNS, which can be unreachable here. Change only this guest's file.
    read -ra dns_servers <<< "${4:?Guest IPv4 DNS servers required}"
    (( ${#dns_servers[@]} > 0 ))
    for dns in "${dns_servers[@]}"; do
        [[ $dns =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || { echo 'DNS servers must be IPv4 addresses.' >&2; exit 1; }
    done
    [[ ! -L /etc/resolv.conf ]]
    printf 'nameserver %s\n' "${dns_servers[@]}" > /etc/resolv.conf
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates python3 python3-venv \
        python3-pip git curl ripgrep nodejs npm procps util-linux libstdc++6
    if ! getent group "$gid" >/dev/null; then groupadd --gid "$gid" daxqo; fi
    if ! id daxqo >/dev/null 2>&1; then
        # v5 injects aid_u0_aNNN with this UID. A second passwd name gives us
        # a private home while keeping the actual Android process identity.
        useradd --non-unique --create-home --uid "$uid" --gid "$gid" --shell /bin/bash daxqo
    fi
    if [[ $(id -u daxqo) != "$uid" || $(id -g daxqo) != "$gid" ]]; then
        echo 'Existing daxqo user has a different identity; nothing was reset.' >&2
        exit 1
    fi
    chmod 700 /home/daxqo
    exit 0
fi
# Using Termux's real UID is essential for runtime_control's /proc checks.
[[ $(id -u) == "$(id -u daxqo)" && $HOME == /home/daxqo ]]
app=$HOME/.local/share/daxqo
python=$app/venv/bin/python
case "$action" in
    install)
        mkdir -p "$app"
        [[ -x $python ]] || python3 -m venv "$app/venv"
        "$python" "$repo/scripts/install_linux.py" --home "$app" --no-start --pairing-scheme daxqo-xm
        "$python" "$repo/scripts/install_mail_deps.py" --app "$app"
        "$python" "$repo/scripts/xm_runtime.py" --app "$app" doctor
        echo 'Installation complete. Run: bash scripts/install_xm.sh start'
        ;;
    start|doctor|status) exec "$python" "$repo/scripts/xm_runtime.py" --app "$app" "$action" ;;
    pair)
        # Explicit user action only; never write the pairing secret to a log.
        exec "$python" -c 'from pathlib import Path; print("daxqo-xm://connect#token=" + (Path.home()/".local/share/daxqo/state/capability").read_text().strip())'
        ;;
    test) cd "$app"; exec "$python" -m unittest test_server test_supervisor test_runtime_control test_task_services test_android_mcp test_remote_protocol test_email_control test_remote_email_mcp ;;
    *) exit 2 ;;
esac
