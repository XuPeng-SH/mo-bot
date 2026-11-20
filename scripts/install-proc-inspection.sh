#!/bin/sh
# Install the narrowly scoped process inspector for one existing local user.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

if [ "$(id -u)" -ne 0 ] || [ "$#" -ne 1 ]; then
    printf '%s\n' 'Usage: sudo scripts/install-proc-inspection.sh USER' >&2
    exit 2
fi
case "$1" in
    ''|*[!a-z0-9_-]*|[0-9-]*) printf '%s\n' 'Invalid local user name' >&2; exit 2 ;;
esac
inspector_user=$1
inspector_uid=$(id -u "$inspector_user")
if [ "$inspector_uid" -eq 0 ]; then
    printf '%s\n' 'The inspector must be assigned to a non-root user' >&2
    exit 2
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
inspector_source=$script_dir/../mo_bot/proc_inspection.py
inspector_dest=/usr/local/libexec/mo-bot-proc-scan
rule_dest=/etc/sudoers.d/mo-bot-proc-scan-$inspector_uid
check_directory() {
    directory=$1
    if [ -L "$directory" ] || [ "$(stat -c %u "$directory")" -ne 0 ]; then
        printf 'Unsafe ownership or symlink: %s\n' "$directory" >&2
        exit 1
    fi
    directory_mode=$(stat -c %a "$directory")
    if [ "$((0$directory_mode & 022))" -ne 0 ]; then
        printf 'Writable by non-root: %s\n' "$directory" >&2
        exit 1
    fi
}
for directory in / /usr /usr/local /etc; do
    check_directory "$directory"
done
mkdir -p /usr/local/libexec /etc/sudoers.d
for directory in /usr/local/libexec /etc/sudoers.d; do
    check_directory "$directory"
done
if [ -L "$inspector_dest" ] || [ -L "$rule_dest" ]; then
    printf '%s\n' 'Refusing to replace a symlink' >&2
    exit 1
fi
if [ -e "$inspector_dest" ] && [ "$(sed -n '2p' "$inspector_dest")" != '"""Inspect process references for task scratch cleanup."""' ]; then
    printf '%s\n' 'Refusing to replace an unrelated inspector' >&2
    exit 1
fi
for existing in "$inspector_dest" "$rule_dest"; do
    if [ -e "$existing" ] && [ "$(stat -c %u "$existing")" -ne 0 ]; then
        printf 'Refusing to replace a non-root-owned file: %s\n' "$existing" >&2
        exit 1
    fi
done
helper_tmp=$(mktemp /usr/local/libexec/.mo-bot-proc-scan.XXXXXXXX)
rule_tmp=$(mktemp /etc/sudoers.d/.mo-bot-proc-scan.XXXXXXXX)
cleanup() {
    if [ -e "$helper_tmp" ]; then unlink "$helper_tmp"; fi
    if [ -e "$rule_tmp" ]; then unlink "$rule_tmp"; fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
install -o root -g root -m 0755 "$inspector_source" "$helper_tmp"
printf '%s ALL=(root) NOPASSWD: %s ""\n' "$inspector_user" "$inspector_dest" > "$rule_tmp"
chmod 0440 "$rule_tmp"
/usr/sbin/visudo -cf "$rule_tmp"
if [ -e "$rule_dest" ] && ! cmp -s "$rule_tmp" "$rule_dest"; then
    printf '%s\n' 'Refusing to replace an existing different sudo rule' >&2
    exit 1
fi
mv -f "$helper_tmp" "$inspector_dest"
mv -f "$rule_tmp" "$rule_dest"
printf '%s\n' 'Installed process inspection helper and restricted sudo rule.'
