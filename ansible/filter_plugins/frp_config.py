"""Service model shared by Ansible and the operator CLI (stdlib only)."""

import base64
import binascii
import ipaddress
import re
from urllib.parse import urlsplit


_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")
_BCRYPT = re.compile(r"\$2[aby]\$(?:0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}\Z")


def _fail(field, reason):
    raise ValueError(f"{field}: {reason}")


def _text(value, field):
    if not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
        _fail(field, "must be a nonempty printable string without whitespace")
    return value


def _keys(value, allowed, required, field):
    if not isinstance(value, dict):
        _fail(field, "must be a mapping")
    if set(value) - allowed:
        _fail(field, "contains unsupported fields")
    if not required <= set(value):
        _fail(field, "missing required fields: " + ", ".join(sorted(required - set(value))))


def _port(value, field, minimum=1024):
    if type(value) is not int or not minimum <= value <= 65535:
        _fail(field, f"must be an integer from {minimum} to 65535")
    return value


def _dns(value, field, fqdn=False):
    value = _text(value, field).lower()
    labels = value.split(".")
    if len(value) > 253 or any(not _LABEL.fullmatch(label) for label in labels):
        _fail(field, "must be a DNS hostname without wildcards")
    if fqdn and (len(labels) < 2 or labels[-1].isdigit()):
        _fail(field, "must be a fully qualified DNS hostname")
    return value


def _host(value, field):
    value = _text(value, field)
    if "%" in value:
        _fail(field, "scoped IP addresses are not supported")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if ":" in value or "%" in value or re.fullmatch(r"[0-9.]+", value):
            _fail(field, "must be a valid IP address or DNS hostname")
        return _dns(value, field)


def _url(value, field, schemes, backend=False):
    value = _text(value, field)
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        _fail(field, "must be a valid absolute URL")
    if parsed.scheme not in schemes or not host or parsed.username is not None or parsed.password is not None:
        _fail(field, "must be an absolute URL with an allowed scheme and no credentials")
    if "?" in value or "#" in value or "\\" in value:
        _fail(field, "must not contain a query, fragment, or backslash")
    host = _host(host, field)
    if backend and (port is None or parsed.path not in ("", "/")):
        _fail(field, "must include an explicit port and no path except /")
    if port is not None:
        _port(port, field, minimum=1)
    return parsed.scheme, host, port


def _validate_oidc(oidc):
    field = "frp_oidc"
    required = {"issuer_url", "client_id", "client_secret", "cookie_secret"}
    _keys(oidc, required, required, field)
    _url(oidc["issuer_url"], field + ".issuer_url", {"https"})
    for key in ("client_id", "client_secret"):
        _text(oidc[key], field + "." + key)
    secret = _text(oidc["cookie_secret"], field + ".cookie_secret")
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", secret):
            raise ValueError
        decoded = base64.b64decode(secret + "=" * (-len(secret) % 4), altchars=b"-_", validate=True)
        if len(decoded) != 32:
            raise ValueError
    except (ValueError, binascii.Error):
        _fail(field + ".cookie_secret", "must be URL-safe base64 encoding exactly 32 random bytes")


def normalize_services(services, reserved_ports=(), oidc=None):
    """Validate a complete service list without echoing input values in errors.

    All relay-side listeners share one collision domain. Backend listener ports
    belong to the client, and deliberately do not participate in that domain.
    """
    if not isinstance(services, list) or not services:
        _fail("frp_services", "must be a nonempty list")
    occupied = set()
    for index, port in enumerate(reserved_ports):
        occupied.add(_port(port, f"reserved_ports[{index}]", minimum=1))
    names, hostnames, result = set(), set(), []
    for index, service in enumerate(services):
        field = f"frp_services[{index}]"
        required = {"name", "protocol", "backend", "relay_port"}
        allowed = required | {"public_port", "hostname", "access"}
        _keys(service, allowed, required, field)
        name = _text(service["name"], field + ".name")
        if len(name) > 63 or not _NAME.fullmatch(name):
            _fail(field + ".name", "must be a lowercase letter-led slug of at most 63 characters")
        if name in names:
            _fail(field + ".name", "duplicates another service name")
        names.add(name)
        protocol = service["protocol"]
        if protocol not in ("tcp", "http"):
            _fail(field + ".protocol", "must be tcp or http")
        item = dict(service)
        ports = ["relay_port"]
        if protocol == "tcp":
            if "public_port" not in service or "hostname" in service or "access" in service:
                _fail(field, "tcp requires public_port and forbids hostname/access")
            backend = _text(service["backend"], field + ".backend")
            if backend.startswith("["):
                match = re.fullmatch(r"\[([^\]]*:[^\]]*)\]:([0-9]{1,5})", backend)
            else:
                match = re.fullmatch(r"([^:]+):([0-9]{1,5})", backend)
            if not match:
                _fail(field + ".backend", "must be HOST:PORT (bracket IPv6 addresses)")
            host = _host(match[1], field + ".backend")
            port = _port(int(match[2]), field + ".backend", minimum=1)
            scheme = "tcp"
            ports.append("public_port")
        else:
            if "hostname" not in service or "access" not in service or "public_port" in service:
                _fail(field, "http requires hostname/access and forbids public_port")
            scheme, host, port = _url(service["backend"], field + ".backend", {"http", "https"}, backend=True)
            hostname = _dns(service["hostname"], field + ".hostname", fqdn=True)
            if hostname in hostnames:
                _fail(field + ".hostname", "duplicates another service hostname")
            hostnames.add(hostname)
            item["hostname"] = hostname
            access = service["access"]
            access_field = field + ".access"
            _keys(access, {"mode", "users", "port", "allowed_emails", "allowed_domains"}, {"mode"}, access_field)
            mode = access["mode"]
            if mode not in ("public", "application", "basic", "oidc"):
                _fail(access_field + ".mode", "must be public, application, basic, or oidc")
            accepted = {"mode"}
            if mode == "basic":
                accepted.add("users")
                users = access.get("users")
                if not isinstance(users, dict) or not users:
                    _fail(access_field + ".users", "must be a nonempty username-to-bcrypt mapping")
                for username, password in users.items():
                    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
                        _fail(access_field + ".users", "contains an unsafe username")
                    if not isinstance(password, str) or not _BCRYPT.fullmatch(password):
                        _fail(access_field + ".users", "requires bcrypt hashes, not plaintext passwords")
            if mode == "oidc":
                accepted.update({"port", "allowed_emails", "allowed_domains"})
                _validate_oidc(oidc)
                listener = _port(access.get("port"), access_field + ".port")
                if listener in occupied:
                    _fail(access_field + ".port", "collides with another relay listener")
                occupied.add(listener)
                restrictions = 0
                normalized_access = dict(access)
                for key in ("allowed_emails", "allowed_domains"):
                    values = access.get(key, [])
                    if not isinstance(values, list):
                        _fail(access_field + "." + key, "must be a list")
                    normalized_values = []
                    for value in values:
                        value = _text(value, access_field + "." + key)
                        if key == "allowed_domains":
                            value = _dns(value, access_field + "." + key, fqdn=True)
                        else:
                            if value.count("@") != 1:
                                _fail(access_field + "." + key, "requires explicit email addresses")
                            local, domain = value.split("@")
                            if not re.fullmatch(r"[A-Za-z0-9.!#$%&'+/=?^_`{|}~-]+", local):
                                _fail(access_field + "." + key, "requires explicit email addresses without wildcards")
                            value = local.lower() + "@" + _dns(domain, access_field + "." + key, fqdn=True)
                        normalized_values.append(value)
                    restrictions += len(normalized_values)
                    if key in access:
                        normalized_access[key] = normalized_values
                if not restrictions:
                    _fail(access_field, "oidc requires allowed_emails and/or allowed_domains restrictions")
                item["access"] = normalized_access
            if set(access) - accepted:
                _fail(access_field, "contains fields not supported by this access mode")
        for key in ports:
            listener = _port(service[key], field + "." + key)
            if listener in occupied:
                _fail(field + "." + key, "collides with another relay listener")
            occupied.add(listener)
        item.update(backend_host=host, backend_port=port, backend_scheme=scheme)
        result.append(item)
    return result


def service_error(services, reserved_ports=(), oidc=None):
    """Return only a safe field error, keeping secrets out of Ansible results."""
    try:
        normalize_services(services, reserved_ports, oidc)
    except ValueError as exc:
        return str(exc)
    return ""


class FilterModule:
    def filters(self):
        return {
            "frp_normalize_services": normalize_services,
            "frp_service_error": service_error,
        }
