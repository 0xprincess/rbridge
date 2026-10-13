#!/usr/bin/env python3
"""Web-first publishing through an existing relay, using the project's Ansible roles."""

import argparse
from contextlib import contextmanager, nullcontext, redirect_stdout
import copy
import fcntl
import getpass
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load the project's Python modules.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tunnel = load_script("tunnel")
generator = load_script("create-secrets")
planning = load_script("bridge_plan")
discovery = load_script("bridge_status")


def workspace_path(value):
    directory = Path(value).expanduser().resolve()
    if os.name != "posix" or str(directory).startswith("/mnt/"):
        raise ValueError("Use a private directory on the Linux/WSL Linux filesystem, not /mnt.")
    if any((ancestor / ".git").exists() for ancestor in (directory, *directory.parents)):
        raise ValueError("Private workspace must be outside every Git checkout.")
    if directory.exists():
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Private workspace must be owned by this user with directory permissions 0700.")
    return directory


def private_read(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("Workspace files must be regular, owned by this user, and private (0600).")
    return path.read_text()


def private_write(path, value):
    """Atomic replacement never temporarily exposes configuration or credentials."""
    descriptor, temporary = tempfile.mkstemp(prefix=".bridge-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def workspace_lock(directory):
    descriptor = os.open(directory / ".bridge.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another rbridge apply is using this workspace; wait for it to finish.") from None
        yield
    finally:
        os.close(descriptor)


def flat_yaml(text):
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError("Cannot read private configuration as YAML.") from None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value) or "all" in value:
        raise ValueError("Service, secret, and controller files must contain flat variable mappings.")
    return value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def load_workspace(directory):
    if not directory.is_dir():
        raise ValueError("Workspace is missing; run rbridge init first.")
    services = flat_yaml(private_read(directory / "services.yml"))
    secrets = flat_yaml(private_read(directory / "secrets.yml"))
    # A hidden overlay would otherwise silently undo publish/remove operations.
    if "frp_services" in secrets or "frp_managed_ssh" in secrets:
        raise ValueError("Keep frp_services and frp_managed_ssh in services.yml, not secrets.yml.")
    inventory = private_read(directory / "inventory.yml")
    known_hosts = private_read(directory / "known_hosts")
    controller_path = directory / "controller.yml"
    controller = flat_yaml(private_read(controller_path)) if controller_path.exists() else {}
    previous_path = directory / "last-applied.json"
    try:
        previous = json.loads(private_read(previous_path)) if previous_path.exists() else None
    except json.JSONDecodeError:
        raise ValueError("Saved deployment baseline is unreadable; preserve it privately before repairing the workspace.") from None
    return {"directory": directory, "variables": services, "secrets": secrets,
            "inventory": inventory, "known_hosts": known_hosts, "controller": controller,
            "previous": previous, "uncertain": (directory / "apply-pending.json").exists()}


def snapshot(workspace, variables=None):
    config = {**(workspace["variables"] if variables is None else variables), **workspace["secrets"]}
    services = tunnel.validate_config(config)
    token = config.get("frp_token")
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ValueError("Private frp_token must be 64 lowercase hexadecimal characters.")
    address = config.get("frp_server_address")
    if not isinstance(address, str):
        raise ValueError("frp_server_address must be the relay IPv4 address or DNS name.")
    address = generator.validate_host(address)
    port = config.get("frp_relay_ssh_port", 22)
    generator.pinned_hosts(workspace["known_hosts"], address, port)
    if config.get("frp_relay_known_hosts") != workspace["known_hosts"]:
        raise ValueError("Relay host pins in secrets.yml and known_hosts must match; verify any rotation independently.")
    return {"config": config, "services": services,
            "manifest_token": tunnel.model.revision_token(token, services, config.get("frp_oidc")),
            "inventory_digest": digest(workspace["inventory"]),
            "connection_digest": digest(workspace["known_hosts"] + json.dumps(workspace["controller"], sort_keys=True))}


def next_relay_port(config, services):
    occupied = {80, 443, config.get("frp_server_port", 7000), config.get("frp_relay_ssh_port", 22),
                config.get("frp_server_admin_port", 17401)}
    for service in services:
        occupied.add(service["relay_port"])
        if service["protocol"] == "tcp":
            occupied.add(service["public_port"])
        if service.get("access", {}).get("mode") == "oidc" and "port" in service["access"]:
            if type(service["access"]["port"]) is int:
                occupied.add(service["access"]["port"])
    for port in range(12000, 65536):
        if port not in occupied:
            return port
    raise ValueError("No configured relay port is available in the automatic 12000-65535 range.")


def publish_variables(workspace, args):
    variables = copy.deepcopy(workspace["variables"])
    current = snapshot(workspace)
    target = args.target
    if target.isdecimal():
        port = int(target)
        if not 1 <= port <= 65535:
            raise ValueError("Local HTTP port must be between 1 and 65535.")
        target = "http://127.0.0.1:" + str(port)
    name = args.name or "web"
    services = variables["frp_services"]
    existing = next((service for service in services if service["name"] == name), None)
    if existing and existing["protocol"] != "http":
        raise ValueError("This name belongs to a TCP service; choose another name or explicitly remove it first.")
    if args.access_file:
        try:
            access = flat_yaml(Path(args.access_file).expanduser().read_text())
        except (OSError, UnicodeError):
            raise ValueError("Cannot read the private access-policy file.") from None
    elif args.access:
        access = {"mode": args.access}
    elif existing:
        access = copy.deepcopy(existing["access"])
    else:
        raise ValueError("New HTTP services require --access public/application or a private --access-file.")
    service = {"name": name, "protocol": "http", "backend": target,
               "hostname": args.hostname or (existing["hostname"] if existing else ""), "access": access,
               "relay_port": existing["relay_port"] if existing else next_relay_port(current["config"], current["services"])}
    # Explicit OIDC port choices reserve space before automatically choosing the FRP port.
    if not existing and access.get("mode") == "oidc" and access.get("port") == service["relay_port"]:
        service["relay_port"] = next_relay_port(current["config"], current["services"] + [service])
    if access.get("mode") == "oidc" and "port" not in access:
        if existing and existing["access"]["mode"] == "oidc":
            access["port"] = existing["access"]["port"]
        else:
            access["port"] = next_relay_port(current["config"], current["services"] + [service])
    if existing:
        services[services.index(existing)] = service
    else:
        services.append(service)
    snapshot(workspace, variables)
    return variables


def remove_variables(workspace, selection):
    variables = copy.deepcopy(workspace["variables"])
    services = variables["frp_services"]
    index = int(selection) - 1 if selection.isdecimal() else next(
        (index for index, service in enumerate(services) if service["name"] == selection), -1)
    if not 0 <= index < len(services):
        raise ValueError("Service not found; use rbridge list --show-endpoints for names or a current one-based service number.")
    services.pop(index)
    if not services:
        variables["frp_managed_ssh"] = False
    snapshot(workspace, variables)
    return variables


def initialize(args, directory):
    if directory.exists():
        raise ValueError("Workspace already exists; use publish, plan, or apply. Existing keys are never overwritten.")
    key = Path(args.admin_key).expanduser().resolve()
    if not key.is_file():
        raise ValueError("Administrative SSH key is missing; provide an existing key separate from the generated transport key.")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", args.admin_user):
        raise ValueError("Invalid administrative SSH account.")
    with tempfile.TemporaryDirectory(prefix="bridge-init-") as temporary:
        services = Path(temporary) / "services.yml"
        private_write(services, "frp_managed_ssh: false\nfrp_services: []\n")
        values = argparse.Namespace(output=str(directory), server_host=args.relay, client_user=None,
                                    known_hosts=args.known_hosts, relay_port=args.relay_port,
                                    admin_user=args.admin_user, no_managed_ssh=True, services_file=str(services))
        previous_umask = os.umask(0o077)
        try:
            with redirect_stdout(io.StringIO()):
                generator.generate(values)
        finally:
            os.umask(previous_umask)
    inventory = yaml.safe_load(private_read(directory / "inventory.yml"))
    inventory["all"]["children"]["frp_servers"]["hosts"]["relay"]["ansible_port"] = args.relay_port
    private_write(directory / "inventory.yml", yaml.safe_dump(inventory, sort_keys=False))
    private_write(directory / "controller.yml", yaml.safe_dump({"admin_key": str(key)}, sort_keys=False))


def controller_command(workspace, directory, *, password=None):
    key_value = workspace["controller"].get("admin_key")
    if not isinstance(key_value, str) or not Path(key_value).expanduser().is_file():
        raise ValueError("Set admin_key in private controller.yml to an existing administrative SSH key.")
    key = str(Path(key_value).expanduser().resolve())
    pins = directory / "known_hosts"
    ssh_arguments = shlex.join(["-F", "/dev/null", "-o", "StrictHostKeyChecking=yes", "-o",
                                "UserKnownHostsFile=" + json.dumps(str(pins)), "-o", "GlobalKnownHostsFile=/dev/null",
                                "-o", "ForwardAgent=no", "-o", "ProxyCommand=none", "-o", "ProxyJump=none",
                                "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"])
    overrides = {"ansible_ssh_args": ssh_arguments, "ansible_ssh_common_args": "", "ansible_ssh_extra_args": "",
                 "ansible_host_key_checking": True, "ansible_ssh_host_key_checking": True,
                 "ansible_private_key_file": key, "ansible_ssh_private_key_file": key}
    if password is not None:
        overrides["ansible_become_password"] = password
    return overrides


def group_hosts(inventory, name, seen=None):
    seen = set() if seen is None else seen
    if name in seen:
        return set()
    seen.add(name)
    group = inventory.get(name, {})
    hosts = set(group.get("hosts", []))
    for child in group.get("children", []):
        hosts.update(group_hosts(inventory, child, seen))
    return hosts


def apply_workspace(workspace, variables, desired, plan, args):
    executable = shutil.which("ansible-playbook")
    inventory_executable = shutil.which("ansible-inventory")
    if not executable or not inventory_executable:
        raise ValueError("Reinstall rbridge with install.sh, or install requirements-dev.txt in the source checkout's active environment.")
    password = None
    if args.ask_become_pass:
        if not sys.stdin.isatty():
            raise ValueError("--ask-become-pass requires an interactive terminal; otherwise configure passwordless sudo.")
        password = getpass.getpass("Sudo password for this deployment: ")
    directory = workspace["directory"]
    # Always show the interruption preview before touching hosts, even with JSON stdout.
    print(planning.format_plan(plan), file=sys.stderr, flush=True)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ANSIBLE_")}
    environment.update(ANSIBLE_CONFIG=str(ROOT / "ansible/ansible.cfg"), ANSIBLE_NOCOLOR="1")
    with tempfile.TemporaryDirectory(prefix=".bridge-apply-", dir=directory) as temporary:
        staged = Path(temporary)
        overrides = controller_command(workspace, staged, password=password)
        private_write(staged / "inventory.yml", workspace["inventory"])
        private_write(staged / "known_hosts", workspace["known_hosts"])
        private_write(staged / "variables.yml", yaml.safe_dump({**desired["config"], **overrides}, sort_keys=False))
        common = ["-i", str(staged / "inventory.yml"), "-e", "@" + str(staged / "variables.yml")]
        # Ansible can return success when no hosts match. Never record that as an apply.
        inspected = subprocess.run([inventory_executable, *common, "--list"], env=environment,
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        try:
            inventory = json.loads(inspected.stdout) if inspected.returncode == 0 else {}
            complete = all(group_hosts(inventory, name) for name in ("frp_servers", "frp_clients"))
        except (ValueError, TypeError, AttributeError):
            complete = False
        if not complete:
            raise ValueError("Inventory must resolve both frp_servers and frp_clients; no deployment was started.")
        private_write(directory / "apply-pending.json", '{"state":"incomplete"}\n')
        # Retain the intended configuration on failure so plain `apply` retries the same revision.
        private_write(directory / "services.yml", yaml.safe_dump(variables, sort_keys=False))
        descriptor = os.open(directory / "apply.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as log:
            os.fchmod(log.fileno(), 0o600)
            print("Applying both hosts; detailed output stays in private apply.log.", file=sys.stderr, flush=True)
            completed = subprocess.run([executable, *common, str(ROOT / "ansible/deploy.yml")],
                                       env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        if completed.returncode != 0:
            return {"ok": False, "applied": False, "deployment": "incomplete", "plan": plan,
                    "error": "Ansible failed. Both hosts may be partially changed; inspect private apply.log, then plan and apply again."}
    private_write(directory / "last-applied.json", json.dumps(desired, sort_keys=True) + "\n")
    (directory / "apply-pending.json").unlink()
    return {"ok": True, "applied": True, "deployment": "applied", "plan": plan,
            "discovery": discovery.describe_services(desired["config"], desired["services"],
                                                     reveal=args.show_endpoints, deployment="applied")}


def public_checks(config, services, timeout):
    checks = []
    for index, service in enumerate(services, 1):
        if service["protocol"] == "http":
            specification = {"kind": "http", "host": service["hostname"], "port": 443, "scheme": "https"}
            access = service["access"]["mode"]
        else:
            specification = {"kind": "tcp", "host": config["frp_server_address"], "port": service["public_port"]}
            access = None
        checks.append(tunnel.endpoint_check(specification, "public_ingress", index, timeout, access))
    return checks


def parser():
    result = argparse.ArgumentParser(prog="rbridge", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""One-time setup (an existing Ubuntu relay, verified host pin and admin key are required):
  rbridge init --relay relay.example.invalid --known-hosts /private/verified-hosts --admin-key /private/admin-key
  rbridge apply --ask-become-pass
Everyday publishing (hostname DNS must point to the relay):
  rbridge publish 3000 --name demo --hostname demo.example.invalid --access public --dry-run
  rbridge publish 3000 --name demo --hostname demo.example.invalid --access public
  rbridge list --show-endpoints
  rbridge status --json
  rbridge remove demo
Publish/remove deploy by default; --dry-run previews without changing files or hosts.
Commands use $PRIVATE or ~/.config/rbridge-private. Endpoint disclosure is opt-in.
Systemd publications outlive this CLI; WSL must remain awake. DNS/firewall/cloud provisioning
and full infrastructure uninstall remain operator-owned. Run tunnel.py for advanced diagnostics.""")
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (("init", "Prepare a web-only private workspace; publish nothing"),
                            ("publish", "Publish a local HTTP service; use --dry-run to preview"),
                            ("plan", "Preview changes against the last successful CLI apply"),
                            ("apply", "Show impact and deploy both hosts through Ansible"),
                            ("list", "List configured services without probing"),
                            ("status", "Check public ingress, or diagnose an explicitly selected local role"),
                            ("remove", "Remove a publication; use --dry-run to preview")):
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument("--private-dir", default=os.environ.get("PRIVATE", str(Path.home() / ".config/rbridge-private")),
                             help="Private workspace (default: $PRIVATE or ~/.config/rbridge-private)")
        command.add_argument("--json", action="store_true", help="One redacted JSON result on stdout; apply previews go to stderr")
        command.add_argument("--show-endpoints", action="store_true", help="Explicitly reveal service names/endpoints and connection instructions locally, never secrets")
        if name in ("publish", "remove", "apply"):
            command.add_argument("--ask-become-pass", action="store_true", help="Prompt privately for sudo password for this apply; never store it in the workspace")
        if name in ("publish", "remove", "apply"):
            command.add_argument("--dry-run", action="store_true", help="Preview only; do not change workspace files or hosts")
        if name == "init":
            command.add_argument("--relay", required=True, help="Existing relay IPv4 address or DNS name")
            command.add_argument("--known-hosts", required=True, help="Previously independently verified Ed25519 relay host pin")
            command.add_argument("--admin-key", required=True, help="Existing relay administrative key, not a generated tunnel key")
            command.add_argument("--admin-user", default="root")
            command.add_argument("--relay-port", type=int, default=22, help="Relay administrative/transport SSH port")
        elif name == "publish":
            command.add_argument("target", help="Local HTTP port, or explicit http(s)://HOST:PORT backend")
            command.add_argument("--name", help="Service name (default: web); repeat a name to update it")
            command.add_argument("--hostname", help="Required for a new service; updates preserve the existing hostname")
            access = command.add_mutually_exclusive_group()
            access.add_argument("--access", choices=("public", "application"), help="Explicit new-service policy; omitted on updates preserves the existing policy")
            access.add_argument("--access-file", help="Private YAML access mapping for any supported mode; Basic requires hashes, OIDC uses secrets.yml")
        elif name == "remove":
            command.add_argument("service", help="Service name or current one-based list index")
        elif name == "status":
            command.add_argument("--timeout", type=float, default=3, help="Per-probe wall-clock timeout, 0.1-30 seconds")
            command.add_argument("--local-role", choices=("client", "server"), help="Additionally diagnose this host's installed role; requires local configuration permissions")
    return result


def execute(args):
    directory = workspace_path(args.private_dir)
    if args.command == "init":
        initialize(args, directory)
    if not directory.is_dir():
        raise ValueError("Workspace is missing; run rbridge init first.")
    deploying = args.command in ("publish", "remove", "apply") and not args.dry_run
    if args.command == "status" and not 0.1 <= args.timeout <= 30:
        raise ValueError("--timeout must be between 0.1 and 30 seconds.")
    with workspace_lock(directory) if deploying else nullcontext():
        workspace = load_workspace(directory)
        variables = workspace["variables"]
        if args.command == "publish":
            variables = publish_variables(workspace, args)
        elif args.command == "remove":
            variables = remove_variables(workspace, args.service)
        desired = snapshot(workspace, variables)
        plan = planning.build_plan(desired, workspace["previous"], uncertain=workspace["uncertain"], reveal=args.show_endpoints)
        if deploying:
            return apply_workspace(workspace, variables, desired, plan, args)
        if args.command in ("list", "status"):
            checks = None
            if args.command == "status":
                checks = (tunnel.doctor(desired["config"], desired["services"], args.local_role, args.timeout)
                          if args.local_role else public_checks(desired["config"], desired["services"], args.timeout))
            deployment = ("incomplete" if workspace["uncertain"] else "not_applied" if workspace["previous"] is None
                          else "unknown" if plan["baseline"] == "unknown" else "changed" if plan["changed"] else "unchanged")
            return discovery.describe_services(desired["config"], desired["services"], checks=checks,
                                               reveal=args.show_endpoints, deployment=deployment)
        return {"ok": True, "initialized": args.command == "init", "applied": False, "plan": plan}


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        result = execute(arguments)
        code = 1 if result.get("ok") is False else 0
    except ValueError as error:
        result, code = {"ok": False, "error": str(error)}, 2
    except (OSError, UnicodeError, subprocess.SubprocessError, yaml.YAMLError):
        result, code = {"ok": False, "error": "Private file or subprocess operation failed; inspect the workspace and private apply.log. An interrupted apply remains uncertain."}, 2
    except KeyboardInterrupt:
        result, code = {"ok": False, "error": "Interrupted. An in-progress deployment remains uncertain; inspect private apply.log, then run plan."}, 130
    if arguments.json:
        print(json.dumps(result))
    elif "error" in result:
        print(result["error"])
    elif "services" in result:
        print(discovery.format_services(result))
    elif result.get("applied"):
        print("Applied both hosts. Publications persist after this command exits.")
        print(discovery.format_services(result["discovery"]))
    else:
        if result.get("initialized"):
            print("Private web-only workspace created. No host changes or publications yet.")
        print(planning.format_plan(result["plan"]))
        print("No host changes. Run apply for current files, or repeat publish/remove without --dry-run.")
    return code


if __name__ == "__main__":
    sys.exit(main())
