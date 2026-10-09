#!/bin/sh
# Install a self-contained, user-local rbridge from this checkout; no sudo needed.
set -eu
umask 077

fail() { printf '%s\n' "rbridge installer: $*" >&2; exit 1; }
usage() {
    printf '%s\n' 'Usage: sh install.sh [--prefix DIR] [--python EXECUTABLE]' \
        'Default prefix: $HOME/.local; installs bin/rbridge and share/rbridge.' \
        'Requires Linux/WSL, Python 3.11+ with venv/pip, OpenSSH, tar and Internet access.' \
        'Run again from an updated checkout to install a new isolated release.'
}
prefix=${HOME:?HOME is required}/.local
python=python3
while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix|--python)
            [ "$#" -ge 2 ] || fail "$1 requires a value"
            [ -n "$2" ] || fail "$1 requires a nonempty value"
            case "$1" in --prefix) prefix=$2 ;; --python) python=$2 ;; esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown argument: $1" ;;
    esac
done
[ "$(uname -s)" = Linux ] || fail 'only Linux and WSL are supported'
for command in "$python" ssh ssh-keygen tar readlink; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done
"$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || fail 'Python 3.11+ is required'
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
[ -f "$source_dir/scripts/bridge.py" ] && [ -f "$source_dir/ansible/deploy.yml" ] || fail 'run install.sh from a complete project checkout'
mkdir -p -- "$prefix"
prefix=$(CDPATH= cd -- "$prefix" && pwd -P)
case "$prefix" in /mnt|/mnt/*) fail 'install on the Linux filesystem, not a WSL /mnt mount' ;; esac
bin=$prefix/bin
data=$prefix/share/rbridge
[ ! -L "$data" ] || fail 'refusing a symlinked installation directory'
if [ -e "$data" ]; then
    [ -f "$data/.rbridge-install" ] && [ "$(cat "$data/.rbridge-install")" = rbridge-install-v1 ] || fail 'refusing to adopt an unrelated installation directory'
fi
if [ -e "$bin/rbridge" ] || [ -L "$bin/rbridge" ]; then
    [ -L "$bin/rbridge" ] && [ "$(readlink -- "$bin/rbridge")" = "$data/current/rbridge" ] || fail 'refusing to replace an unrelated bin/rbridge'
fi
mkdir -p -- "$bin" "$data/releases"
printf '%s\n' rbridge-install-v1 > "$data/.rbridge-install"
# A permanent release path keeps virtualenv shebangs valid. Switch only after
# dependencies and the CLI are usable; never move a populated virtualenv.
release=$(mktemp -d "$data/releases/release.XXXXXXXX")
link=$release/current-link
activated=false
cleanup() {
    if [ "$activated" = false ]; then rm -rf -- "$release"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p -- "$release/scripts" "$release/ansible/filter_plugins"
for script in bridge.py bridge_plan.py bridge_status.py tunnel.py create-secrets.py; do
    cp -- "$source_dir/scripts/$script" "$release/scripts/$script"
done
cp -- "$source_dir/ansible/deploy.yml" "$source_dir/ansible/ansible.cfg" "$release/ansible/"
cp -- "$source_dir/ansible/filter_plugins/frp_config.py" "$release/ansible/filter_plugins/"
# Only runtime assets are copied: never inventory, .env, secrets, logs or Git data.
tar -C "$source_dir" --exclude=__pycache__ --exclude='*.pyc' -cf "$release/roles.tar" ansible/roles
tar -C "$release" -xf "$release/roles.tar"
rm -- "$release/roles.tar"
cp -- "$source_dir/requirements-dev.txt" "$source_dir/LICENSE" "$release/"
"$python" -m venv "$release/venv" || fail 'cannot create a virtualenv; install the OS python3-venv package'
"$release/venv/bin/python" -m pip install --disable-pip-version-check -r "$release/requirements-dev.txt"
cat > "$release/rbridge" <<'LAUNCHER'
#!/bin/sh
set -eu
# Resolve the selected release before starting so updates cannot mix its assets.
here=$(dirname -- "$(readlink -f -- "$0")")
PATH="$here/venv/bin:$PATH"
export PATH
exec "$here/venv/bin/python" -I "$here/scripts/bridge.py" "$@"
LAUNCHER
chmod 755 "$release/rbridge"
"$release/rbridge" --help >/dev/null
ln -s -- "$release" "$link"
mv -Tf -- "$link" "$data/current"
activated=true
if [ ! -L "$bin/rbridge" ]; then
    ln -s -- "$data/current/rbridge" "$bin/rbridge"
fi
printf 'Installed rbridge: %s\n' "$bin/rbridge"
printf 'Add %s to PATH if needed, then run: rbridge --help\n' "$bin"
printf '%s\n' 'No relay, workspace, DNS or firewall changes were made. Older releases are retained.'
