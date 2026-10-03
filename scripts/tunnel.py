#!/usr/bin/env python3
"""Validate service configuration and diagnose an installed tunnel without changes."""

import argparse
import base64
import http.client
import importlib.util
import json
import multiprocessing
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tomllib

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("frp_config", ROOT / "ansible/filter_plugins/frp_config.py")
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)

PORT_DEFAULTS = {
    "frp_server_port": 7000, "frp_relay_ssh_port": 22,
    "frp_forward_port": 17001, "frp_backend_ssh_port": 2222,
    "frp_client_admin_port": 17400, "frp_server_admin_port": 17401,
}
LOGS = {
    "client": "sudo journalctl -u frp-tunnel-transport -u frp-tunnel-client -u frp-tunnel-sshd --since '-10 min' --no-pager",
    "server": "sudo journalctl -u frp-tunnel-server -u frp-tunnel-web -u 'frp-tunnel-oidc-*' --since '-10 min' --no-pager",
}


def load_config(paths):
    """Load ordered, flat variable mappings, never Ansible inventory or templates."""
    config = {}
    for path in paths:
        try:
            value = yaml.safe_load(Path(path).expanduser().read_text())
        except (OSError, UnicodeError, yaml.YAMLError):
            raise ValueError("Cannot read configuration as YAML; check the private file and permissions.") from None
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise ValueError("Configuration must be a flat YAML mapping of Ansible variables.")
        if "all" in value:
            raise ValueError("Use flat service variables, not inventory.yml; pass services.yml and secrets.yml separately.")
        config.update(value)
    return config


def validate_config(config):
    managed = config.get("frp_managed_ssh", True)
    if type(managed) is not bool:
        raise ValueError("frp_managed_ssh must be a YAML boolean.")
    ports = {}
    for key, default in PORT_DEFAULTS.items():
        if key == "frp_backend_ssh_port" and not managed:
            continue
        value = config.get(key, default)
        minimum = 1 if key == "frp_relay_ssh_port" else 1024
        if type(value) is not int or not minimum <= value <= 65535:
            raise ValueError(key + " must be an integer port from " + str(minimum) + " to 65535.")
        ports[key] = value
    relay = [ports[key] for key in ("frp_server_port", "frp_relay_ssh_port", "frp_server_admin_port")] + [80, 443]
    if len(set(relay)) != len(relay):
        raise ValueError("Relay control, SSH, admin and web listener ports must be distinct.")
    client = [ports["frp_forward_port"], ports["frp_client_admin_port"]]
    if managed:
        client.append(ports["frp_backend_ssh_port"])
    if len(set(client)) != len(client):
        raise ValueError("Client forward, admin and managed SSH listener ports must be distinct.")
    return model.normalize_services(config.get("frp_services"), reserved_ports=relay, oidc=config.get("frp_oidc"))


def admin_settings(config, role, path=None):
    """Only loopback admin requests may carry the private API credentials."""
    source = Path(path) if path else Path("/etc/frp-tunnel/" + ("frpc.toml" if role == "client" else "frps.toml"))
    try:
        with source.open("rb") as stream:
            settings = tomllib.load(stream).get("webServer", {})
    except FileNotFoundError:
        if path:
            raise ValueError("FRP configuration is missing; select the installed --frp-config file.") from None
        settings = {"addr": "127.0.0.1", "port": config.get("frp_" + role + "_admin_port", PORT_DEFAULTS["frp_" + role + "_admin_port"]),
                    "user": config.get("frp_admin_user", "frp-doctor"), "password": config.get("frp_admin_password")}
    except (OSError, ValueError):
        raise ValueError("Cannot read installed FRP configuration; run doctor with appropriate local permissions.") from None
    if not isinstance(settings, dict) or settings.get("addr") not in ("127.0.0.1", "::1"):
        raise ValueError("FRP admin API must bind explicitly to loopback.")
    if type(settings.get("port")) is not int or not 1 <= settings["port"] <= 65535:
        raise ValueError("FRP admin API needs a valid configured port.")
    if not all(isinstance(settings.get(key), str) and settings[key] for key in ("user", "password")):
        raise ValueError("FRP admin credentials are missing; supply private secrets.yml or the installed FRP config.")
    return settings


def http_request(host, port, scheme, timeout, *, connect_host=None, path="/", headers=None, json_body=False):
    """No redirects or environment proxies; TLS verifies the origin identity even via relay."""
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        raw = socket.create_connection((connect_host or host, port), timeout=timeout)
        connection.sock = raw
        if scheme == "https":
            connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        connection.request("GET", path, headers={"Connection": "close", **(headers or {})})
        response = connection.getresponse()
        result = {"code": response.status,
                  "basic_challenge": response.getheader("WWW-Authenticate", "").lower().startswith("basic "),
                  "redirect": bool(response.getheader("Location"))}
        if json_body and response.status == 200:
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("API response too large")
            result["body"] = json.loads(data)
        return result
    finally:
        connection.close()


def _probe(specification, timeout):
    kind = specification["kind"]
    if kind == "tcp":
        with socket.create_connection((specification["host"], specification["port"]), timeout=timeout):
            return {"connected": True}
    return http_request(specification["host"], specification["port"], specification.get("scheme", "http"), timeout,
                        connect_host=specification.get("connect_host"), path=specification.get("path", "/"),
                        headers=specification.get("headers"), json_body=kind == "admin")


def _probe_worker(channel, specification, timeout):
    try:
        channel.send(_probe(specification, timeout))
    except ssl.SSLCertVerificationError:
        channel.send({"error": "tls"})
    except (OSError, ValueError, http.client.HTTPException):
        channel.send({"error": "connection"})
    finally:
        channel.close()


def probe(specification, timeout):
    # A process deadline also bounds DNS and slow/dripping HTTP headers, unlike a socket timeout.
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_probe_worker, args=(sender, specification, timeout))
    process.start()
    sender.close()
    try:
        if receiver.poll(timeout):
            try:
                return receiver.recv()
            except EOFError:
                return {"error": "connection"}
        return {"error": "timeout"}
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join()


def registered_names(response, role):
    if response.get("code") != 200 or not isinstance(response.get("body"), dict):
        return None
    rows = response["body"].get("tcp" if role == "client" else "proxies")
    # FRP returns null for an empty proxy list.
    if rows is None:
        rows = []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    expected = "running" if role == "client" else "online"
    return {row["name"] for row in rows if isinstance(row.get("name"), str) and row.get("status") == expected and not row.get("err")}


def transport_state(timeout):
    try:
        result = subprocess.run(["systemctl", "is-active", "frp-tunnel-transport.service"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    state = result.stdout.strip()
    if result.returncode == 0 and state == b"active":
        return "active"
    if state in (b"inactive", b"failed", b"activating", b"deactivating"):
        return "stopped"
    return "unknown"


def check(stage, status, message, action="", service=None):
    result = {"stage": stage, "status": status, "message": message, "action": action}
    if service is not None:
        result["service"] = service
    return result


def endpoint_check(specification, stage, index, timeout, access=None):
    result = probe(specification, timeout)
    action = "Check the backend process and local firewall." if stage == "backend" else "Check DNS, public firewall, publishing units and Caddy logs; repeat from an external network."
    if "error" in result:
        message = {"tls": "TLS certificate verification failed.", "timeout": "Connection or HTTP response exceeded the deadline."}.get(result["error"], "Connection failed or no valid HTTP response was received.")
        return check(stage, "fail", message, action, index)
    if specification["kind"] == "tcp":
        return check(stage, "ok", "TCP connection accepted; this is not an application-protocol health check.", "Use a protocol-specific client to verify application behavior.", index)
    code = result.get("code", 0)
    if access == "basic":
        success = code == 401 and result.get("basic_challenge", False)
        message = "HTTPS Basic authentication challenge received; authenticated content was not tested."
    elif access == "oidc":
        success = code in (302, 303, 307) and result.get("redirect", False)
        message = "HTTPS login redirect received; browser login and identity authorization were not tested."
    else:
        authentication_required = code in (401, 403) and (stage == "backend" or access == "application")
        success = 200 <= code < 400 or authentication_required
        message = "HTTP response received" + (" (application authentication required)." if authentication_required else ".")
    if not success:
        message = "HTTP response did not meet the configured access policy (status " + str(code) + ")."
    return check(stage, "ok" if success else "fail", message, "" if success else action, index)


def doctor(config, services, role, timeout=3, frp_config=None):
    checks = []
    try:
        admin = admin_settings(config, role, frp_config)
        authorization = base64.b64encode((admin["user"] + ":" + admin["password"]).encode()).decode()
        response = probe({"kind": "admin", "host": admin["addr"], "port": admin["port"],
                          "path": "/api/status" if role == "client" else "/api/proxy/tcp",
                          "headers": {"Authorization": "Basic " + authorization}}, timeout)
        registered = registered_names(response, role)
        registration_error = "Cannot query authenticated FRP status; check credentials, admin listener and FRP logs."
    except ValueError as error:
        registered = None
        registration_error = str(error)
    state = transport_state(timeout) if role == "client" else "unknown"
    has_registered = registered is not None and any(service["name"] in registered for service in services)
    if role == "client" and state == "stopped":
        checks.append(check("transport", "fail", "SSH transport unit is not active.", LOGS[role]))
    elif has_registered:
        checks.append(check("transport", "ok", "A configured proxy is registered over the SSH transport."))
    else:
        checks.append(check("transport", "unknown", "No registered proxy proves end-to-end SSH transport; an active process or listener alone is insufficient.", "Inspect transport logs on the client: " + LOGS["client"]))
    for index, service in enumerate(services, 1):
        good = registered is not None and service["name"] in registered
        checks.append(check("registration", "ok" if good else "fail", "FRP reports this proxy registered." if good else registration_error if registered is None else "FRP does not report this proxy registered.", "" if good else LOGS[role], index))
        backend = {"kind": service["protocol"], "host": service["backend_host"], "port": service["backend_port"], "scheme": service["backend_scheme"]}
        if service["protocol"] == "http":
            backend["headers"] = {"Host": service["hostname"]}
        if role == "server":
            backend.update(port=service["relay_port"], connect_host="127.0.0.1")
            if service["protocol"] == "tcp":
                backend["host"] = "127.0.0.1"
        backend_check = endpoint_check(backend, "backend", index, timeout)
        if role == "server" and service["protocol"] == "tcp" and backend_check["status"] == "ok":
            backend_check = check("backend", "unknown", "FRPS relay accepted TCP; origin backend reachability is unproven.",
                                  "Run doctor --role client on the origin to check local TCP connectivity, then use a protocol-specific client through public ingress to verify end-to-end application behavior.", index)
        checks.append(backend_check)
        if service["protocol"] == "tcp":
            host = config.get("frp_server_address")
            if not isinstance(host, str) or not host or any(character.isspace() for character in host):
                checks.append(check("public_ingress", "fail", "Public TCP checks require frp_server_address in the private configuration.", "Add the relay DNS name or address to services.yml.", index))
                continue
            public = {"kind": "tcp", "host": host, "port": service["public_port"]}
            access = None
        else:
            public = {"kind": "http", "host": service["hostname"], "port": 443, "scheme": "https"}
            access = service["access"]["mode"]
        checks.append(endpoint_check(public, "public_ingress", index, timeout, access))
    return {"ok": all(item["status"] == "ok" for item in checks), "role": role, "checks": checks}


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""Run from the repository on the installed Linux client or relay (Python 3.11+, PyYAML):
  python3 scripts/tunnel.py validate --config /private/services.yml --config /private/secrets.yml
  sudo python3 scripts/tunnel.py doctor --config /private/services.yml --role client --json
  sudo python3 scripts/tunnel.py doctor --config /private/services.yml --role server --json
Use flat Ansible variables, not inventory.yml. Repeated --config files overlay in order.
The installed /etc/frp-tunnel/frpc.toml or frps.toml supplies loopback admin credentials.
If absent, supply secrets.yml too. Files and output should remain private.
Doctor only reads status and makes bounded TCP connections / HTTP GET / requests.
It never follows redirects, submits passwords, mutates services or completes OIDC login.
Server HTTP backend checks traverse the relay; run on the client to isolate local failure.
Server TCP backend checks are 'unknown' even when the relay accepts: FRPS acceptance
does not prove origin reachability. Run doctor --role client on the origin and use a
protocol-specific client through public ingress to verify end-to-end application behavior.
Any failed or unknown check makes doctor exit nonzero, including healthy generic TCP
services checked on the server.
Public checks use this machine's network; repeat externally to detect ingress firewall issues.
An 'ok' TCP check proves a handshake only, not backend/application health.
Logs (review privately; journals may contain endpoints):
  """ + LOGS["client"] + "\n  " + LOGS["server"])
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("validate", "doctor"):
        command = commands.add_parser(name, help="Validate the shared service model" if name == "validate" else "Diagnose transport, registration, backends and public ingress", description="See scripts/tunnel.py --help for repository invocation, file formats, limitations and log commands.")
        command.add_argument("--config", action="append", required=True, metavar="FILE", help="Private flat YAML variables; repeat to overlay secrets or host-specific variables")
        command.add_argument("--json", action="store_true", help="Emit safe machine-readable output; service numbers follow configuration order")
        if name == "doctor":
            command.add_argument("--role", required=True, choices=("client", "server"), help="Role installed on the machine running this command")
            command.add_argument("--frp-config", metavar="FILE", help="Installed FRP TOML override (default /etc/frp-tunnel/frpc.toml or frps.toml)")
            command.add_argument("--timeout", type=float, default=3, help="Per-check wall-clock deadline in seconds, including DNS (0.1–30; default 3)")
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "doctor" and not 0.1 <= arguments.timeout <= 30:
            raise ValueError("--timeout must be between 0.1 and 30 seconds.")
        config = load_config(arguments.config)
        services = validate_config(config)
        if arguments.command == "validate":
            result = {"ok": True, "service_count": len(services)}
        else:
            result = doctor(config, services, arguments.role, arguments.timeout, arguments.frp_config)
    except ValueError as error:
        result = {"ok": False, "error": str(error)}
        print(json.dumps(result) if arguments.json else "Invalid configuration: " + result["error"])
        return 2
    if arguments.json:
        print(json.dumps(result))
    elif arguments.command == "validate":
        print("Configuration valid: " + str(len(services)) + " services.")
    else:
        for item in result["checks"]:
            label = item["stage"] + (" service #" + str(item["service"]) if "service" in item else "")
            print(item["status"].upper() + " " + label + ": " + item["message"])
            if item["action"]:
                print("  " + item["action"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
