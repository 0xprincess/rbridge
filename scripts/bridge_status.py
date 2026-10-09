"""Credential-free views of configured publications and explicitly scoped checks."""

import ipaddress
import re
import shlex


NOTES = (
    "Configured services are not verified deployment; the last successful CLI apply is only a baseline, not live state.",
    "An ok check means only that its stated scope passed, not that the application is healthy.",
    "A TCP handshake does not prove application or SSH login health.",
    "For SSH, explicitly select the correct private login key and an independently verified backend host-key pin; the displayed command supplies neither and does not prove a working login.",
    "Authentication challenges and redirects do not prove authenticated application or login health.",
    "Public probes see only the caller's network; repeat from the intended external network.",
    "Exiting the CLI process does not stop systemd publications; remove and apply to withdraw them.",
    "WSL sleep or shutdown stops availability even when systemd units are enabled.",
)
STAGES = {
    "transport": "SSH transport evidence",
    "registration": "FRP proxy registration",
    "backend": "Backend connectivity/response",
    "public_ingress": "Public ingress connectivity/access-policy response",
}
DEPLOYMENTS = {"unknown", "not_applied", "applied", "pending", "changed", "unchanged", "incomplete"}


def _safe_check(record):
    # Diagnostics may someday include exception strings or remote responses. Never
    # copy free-form diagnostic text into a credential-free discovery view.
    stage = record.get("stage")
    stage = stage if stage in STAGES else "unknown"
    status = record.get("status")
    status = status if status in ("ok", "fail", "unknown") else "unknown"
    description = STAGES.get(stage, "Unrecognized check scope")
    if stage == "unknown":
        status = "unknown"
    return {"stage": stage, "status": status,
            "message": description + ": " + status + ".",
            "action": "" if status == "ok" else "Inspect private diagnostics for this scope; repeat the check after correcting the cause."}


def _host(value):
    """Accept only address material, never userinfo, URLs, options or commands."""
    if not isinstance(value, str) or not value or "%" in value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        labels = value.split(".")
        if len(value) <= 253 and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
            return value
    return None


def _connection(config, service):
    if service["protocol"] == "http":
        endpoint = "https://" + service["hostname"] + "/"
        return endpoint, shlex.join(["curl", "--show-error", "--include", "--", endpoint])
    host = _host(config.get("frp_server_address"))
    if host is None:
        return None, None
    port = service["public_port"]
    endpoint = ("[" + host + "]" if ":" in host else host) + ":" + str(port)
    user = config.get("frp_login_user")
    managed = (config.get("frp_managed_ssh", True) is True
               and service["backend_host"] == "127.0.0.1"
               and service["backend_port"] == config.get("frp_backend_ssh_port", 2222)
               and isinstance(user, str) and bool(re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", user))
               and user not in ("root", config.get("frp_service_user", "frp-tunnel"), config.get("frp_relay_user", "frp-relay"))
               and isinstance(config.get("frp_login_public_key"), str)
               and bool(re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?", config["frp_login_public_key"])))
    command = shlex.join(["ssh", "-p", str(port), "-l", user, "--", host]) if managed else None
    return endpoint, command


def describe_services(config, services, *, checks=None, reveal=False, deployment="unknown"):
    """Describe normalized shared-model services without performing any I/O.

    ``checks=None`` is an unprobed list, unlike an empty collection of requested
    checks (unknown for every configured service). A doctor result mapping or a
    collection of its records is accepted. Global checks affect overall success,
    but cannot stand in for a service-specific check.
    """
    checked = checks is not None
    if isinstance(checks, dict):
        checks = checks.get("checks", [])
    rows = []
    associated = {index: [] for index in range(1, len(services) + 1)}
    global_checks = []
    for record in checks or []:
        if not isinstance(record, dict):
            global_checks.append(_safe_check({}))
            continue
        item = _safe_check(record)
        index = record.get("service")
        if type(index) is int and index in associated:
            associated[index].append(item)
        else:
            # An invalid index must not accidentally make a checked run green.
            if "service" in record:
                item["status"] = "unknown"
                item["message"] = "Check could not be associated with a configured service."
            global_checks.append(item)
    for index, service in enumerate(services, 1):
        items = associated[index]
        statuses = {item["status"] for item in items}
        health = ("not_checked" if not checked else "fail" if "fail" in statuses
                  else "unknown" if not items or "unknown" in statuses else "ok")
        endpoint, command = _connection(config, service) if reveal else ("[redacted]", None)
        rows.append({"service": index, "name": service["name"] if reveal else "[redacted]",
                     "protocol": service["protocol"],
                     "access": service["access"]["mode"] if service["protocol"] == "http" else "application",
                     "health": health, "scope": sorted({item["stage"] for item in items}),
                     "checks": items, "endpoint": endpoint, "command": command})
    return {"ok": (all(row["health"] == "ok" for row in rows)
                   and all(item["status"] == "ok" for item in global_checks)) if checked else None,
            "checked": checked, "deployment": deployment if deployment in DEPLOYMENTS else "unknown",
            "revealed": bool(reveal), "services": rows, "checks": global_checks, "notes": list(NOTES)}


def format_services(result):
    """Render only the safe, already-disclosed discovery result."""
    lines = ["Configured publications (deployment: " + result["deployment"] + ")"]
    if not result["services"]:
        lines.append("No configured publications.")
    for row in result["services"]:
        scope = ", ".join(row["scope"]) or "none"
        lines.append(f"#{row['service']} {row['name']} | {row['protocol']} | access: {row['access']} | check: {row['health']} | scope: {scope}")
        lines.append("  Endpoint: " + (row["endpoint"] or "unavailable (relay address not configured)"))
        if row["command"]:
            lines.append("  Connect: " + row["command"])
        elif result["revealed"] and row["protocol"] == "tcp":
            lines.append("  Use the application's native client; TCP alone does not identify SSH.")
        for check in row["checks"]:
            lines.append("  " + check["message"])
    for check in result["checks"]:
        lines.append("Global: " + check["message"])
    lines.extend("Note: " + note for note in result["notes"])
    return "\n".join(lines)
