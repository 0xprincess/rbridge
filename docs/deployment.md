# Deployment

## Supported setup

The Linux/WSL controller needs Python 3.11+, OpenSSH and the pinned
`ansible-core` in `requirements-dev.txt`. Managed hosts require Ubuntu 24.04+,
systemd, OpenSSH and amd64/arm64. Native Windows services and other distributions
are outside this deployment's scope. Enable
[WSL systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd) first; these
roles do not edit WSL boot configuration or shut down distributions.

Administrators need root/sudo on both hosts. When `frp_managed_ssh` is true, the
client login account must exist, be non-root, have a usable shell, and satisfy
local PAM policy. With managed SSH disabled, applications can be published
without creating or configuring a login account.

Reserve the selected ports before deployment. Relay administration SSH, FRP
control/admin, every service relay port, every public TCP port and every OIDC
port must be distinct. Web ingress reserves 80/443. Client-side forward/admin
and managed SSH ports must be distinct from one another; those are a separate
machine's namespace, so client SSH and relay public SSH may both use 2222.
FRP and application ports are at least 1024; only Caddy receives the capability
to bind privileged web ports.

The relay's native SSH daemon must permit public-key authentication and local
forwarding for the dedicated relay account. Existing `AllowUsers`, `AllowGroups`,
`DisableForwarding`, `AuthorizedKeysFile`, non-PAM account policies or `Match`
blocks can conflict. The main configuration must include
`/etc/ssh/sshd_config.d/*.conf`. Resolve conflicts deliberately, retaining an
administration session.

The role installs an account-scoped `00-frp-tunnel.conf` and a key with
`restrict,port-forwarding` and one `permitopen` destination: loopback FRP control.
The account has a locked password and `nologin` shell. Command execution, remote
forwarding and Unix socket forwarding are denied. The complete native SSH
configuration and effective account policy are checked before reload.

## Private configuration

Keep these outside every checkout:

- `inventory.yml`: managed hosts, administrative connection and sudo settings.
- `services.yml`: editable flat variables including `frp_services`,
  `frp_server_address`, `frp_managed_ssh` and the optional client login identity.
- `secrets.yml`: relay/login keys, trusted relay host pins, FRP/admin credentials
  and, when used, OIDC provider secrets.

Generate them using `scripts/create-secrets.py` as shown in the README. Use
`--services-file FILE` for an existing flat service configuration and
`--no-managed-ssh` to omit login-key generation. The generator validates the
shared model before creating its destination and refuses existing directories.
Public keys, hashes, host pins and provider identifiers also belong in private
inputs; they can identify a deployment.

Validate and deploy the same model:

```bash
python scripts/tunnel.py validate \
  --config "$PRIVATE/services.yml" --config "$PRIVATE/secrets.yml"
ansible-playbook -i "$PRIVATE/inventory.yml" \
  -e "@$PRIVATE/services.yml" -e "@$PRIVATE/secrets.yml" ansible/deploy.yml
```

Repeated CLI `--config` files overlay in order. Do not pass an Ansible inventory
to the CLI: it consumes flat YAML variables, not inventory nesting or Jinja
expressions. The Ansible playbook has a fatal preflight across selected hosts
before provisioning. `--limit` validates only the selected hosts; this is not a
cross-host transaction or a guarantee that every later binary/service operation
will succeed.

### Service fields

| Field | Meaning |
| --- | --- |
| `name` | Unique lowercase, letter-led slug, up to 63 characters |
| `protocol` | `tcp` or `http`; HTTPS backends also use `http` |
| `backend` | TCP `HOST:PORT`, or explicit `http://HOST:PORT` / `https://HOST:PORT` |
| `relay_port` | Unique relay-loopback port, 1024–65535 |
| `public_port` | TCP only: explicit public listener, 1024–65535 |
| `hostname` | HTTP only: unique DNS hostname; no wildcards |
| `access` | HTTP only: explicit policy described below |

Backend URLs cannot contain credentials, a query/fragment, or a path other than
`/`. IPv6 backend addresses use brackets. Backend targets can be local or
reachable LAN hosts; the SSH tunnel protects origin-to-relay traffic, not an
unencrypted LAN hop from the origin to another backend.

Each service becomes a raw FRP TCP proxy bound to relay loopback. TCP services
add `frp-tunnel-publish-NAME.socket` and `.service`, using systemd-socket-proxyd.
HTTP services do not get public raw listeners: Caddy alone forwards to them.
Removing a service removes its generated publication units and FRP listener.
Raw TCP publishing does not add TLS, authentication, or application authorization.
Public TCP publication is currently IPv4; backend IPv6 support does not imply
public IPv6 listeners.

### Infrastructure variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `frp_server_address` | required | Relay IPv4 address or DNS name |
| `frp_managed_ssh` | true | Install the isolated key-only SSH service |
| `frp_login_user` | required with managed SSH | Existing client login account |
| `frp_backend_ssh_port` | 2222 | Managed SSH on client loopback |
| `frp_server_port` | 7000 | Relay-loopback FRP control |
| `frp_forward_port` | 17001 | Client-loopback SSH forwarding |
| `frp_relay_ssh_port` | 22 | Existing relay SSH service |
| `frp_client_admin_port` | 17400 | Authenticated client-loopback status API |
| `frp_server_admin_port` | 17401 | Authenticated relay-loopback status API |
| `frp_admin_user` | frp-doctor | Local status API identity |
| `frp_service_user` | frp-tunnel | FRP/transport account |
| `frp_relay_user` | frp-relay | Restricted relay forwarding account |
| `frp_version` | 0.71.0 | Pinned FRP release |
| `frp_web_tls` | acme | Public certificates; `internal` is explicit test/private-PKI mode |
| `frp_web_acme_email` | empty | Optional ACME contact |
| `frp_web_acme_ca` | empty | Optional alternative/staging ACME directory |

`frp_token` and `frp_admin_password` are separate 32-byte random values encoded
as 64 lowercase hex characters. Other generated secrets are
`frp_relay_private_key`, `frp_relay_public_key`, `frp_login_public_key` and
`frp_relay_known_hosts`. Only the relay private key is installed on the origin;
the user's login private key stays with the connecting user. When managed SSH is
disabled, its unit, configuration and authorized key are removed, not native SSH.

## HTTPS and DNS

Point every published hostname to the relay. Permit inbound TCP 80/443 and allow
outbound DNS/ACME traffic. Caddy obtains and renews certificates automatically;
HTTP redirects to HTTPS. Configure only DNS you control and can validate.
Wildcards and DNS-provider API automation are not implemented.

The dedicated `frp-tunnel-web` unit uses its own pinned Caddy binary and
`/etc/frp-tunnel/web/Caddyfile`; it does not import/adopt `/etc/caddy` or replace
an unrelated Caddy service. An existing listener on 80/443 must be handled by the
administrator. Certificates persist in `/var/lib/frp-tunnel-web`, including when
the final web publication is removed, so later re-enablement retains state.

HTTPS terminates at the relay. The relay can read HTTP traffic; it is not an
end-to-end encrypted web service from browser to origin. HTTPS backends still
receive certificate-chain and hostname verification using the `backend` host as
TLS identity. Install a private backend CA in the relay's normal OS trust store
when appropriate; there is no insecure-verification switch. The application
receives its public hostname as `Host`. WebSockets and streaming are supported.

`frp_web_tls: internal` uses Caddy's local CA, not a browser-trusted public
certificate. Distribute and trust its root deliberately; the role does not
silently change system/browser trust. Its root is at
`/var/lib/frp-tunnel-web/data/caddy/pki/authorities/local/root.crt`.

## Web access policies

Each HTTP service must select exactly one mode:

- `access: {mode: public}`: anyone can reach the backend. Choose deliberately.
- `access: {mode: application}`: the backend owns authentication. Authorization
  headers are preserved. The tunnel does not implement or verify your app's login.
- `mode: basic`: add `access.users`, a mapping of usernames to bcrypt hashes.
  Generate hashes privately with Caddy's interactive `hash-password --algorithm
  bcrypt` command (pinned binary under `/usr/local/lib/frp-tunnel/caddy-2.11.7`).
  Do not pass plaintext passwords through command-line arguments. Caddy validates
  credentials over HTTPS, removes the incoming Authorization header, and forwards
  its authenticated username in `X-Auth-Request-User`.
- `mode: oidc`: add a unique loopback `access.port` and nonempty
  `access.allowed_emails` and/or `access.allowed_domains`. Restrictions are an OR:
  an allowed email **or** an allowed domain permits access. Wildcards are rejected.

OIDC also requires a global `frp_oidc` mapping in private configuration:
`issuer_url` (HTTPS), `client_id`, `client_secret`, and `cookie_secret` (URL-safe
base64 encoding of 32 random bytes). Register **each** app's exact callback
`https://APP_HOSTNAME/oauth2/callback` with the provider. The provider must support
OIDC discovery, authorization-code flow, verified email, nonce validation, and
S256 PKCE. There is no custom identity provider or user database in this project.
If your issuer itself is behind this tunnel, bootstrap its public route before
enabling apps that depend on it.

Each app uses a separate loopback oauth2-proxy instance and host-only
`__Host-frp-NAME` Secure/HttpOnly/SameSite=Lax session cookie. Sessions last up to
one hour; provider tokens are not retained for refresh. Account revocation at
the provider need not invalidate an existing session immediately. OIDC is browser
login, not a substitute for API service-token authentication; use application-owned
authentication for those APIs.

Incoming spoofable identity/forwarding headers are stripped; OIDC forwards only
verified `X-Auth-Request-User` and `X-Auth-Request-Email`. `/oauth2/` is reserved on
OIDC-protected hosts. Authenticator outages fail closed. Keep backend ports
private; directly reachable backends could bypass these controls.

## Upgrades and rollback

Keep administrative access and prior private inputs before deploying. Fresh
installs require unused dedicated accounts and refuse unmarked project
configuration. Managed account names cannot be reassigned in place. Reapplying
identical inputs is intended to produce zero changes; runtime configuration is
validated before replacement. Service/key changes may disconnect sessions.

For an existing `v0.1` installation:

1. Preserve the old inventory/secrets outside the checkout.
2. Replace `frp_public_ssh_port` with the named SSH `frp_services` entry shown in
   the README. Choose a free, separate `relay_port` (for example 12222).
3. Add a fresh `frp_admin_password` to private secrets. Keep existing relay/login
   keys, token and pins unless deliberately rotating them on both hosts.
4. Validate and deploy both hosts. The new FRP server moves application listeners
   to loopback before enabling public socket proxies.

There is no legacy `frp_public_ssh_port` alias. The tag preserves source, not a
snapshot of installed machines. To inspect/redeploy the baseline:

```bash
git worktree add --detach ../frp-tunnel-v0.1 v0.1
```

Before running the old roles, stop and disable the newer relay publication and
web/authenticator units (`frp-tunnel-publish-*.socket`,
`frp-tunnel-publish-*.service`, `frp-tunnel-web.service`,
`frp-tunnel-oidc-*.service`). Review installed units and act only on this project's
owned resources. Remove their owned unit files and run `systemctl daemon-reload`
so a later reboot cannot restore conflicting listeners. Redeploy `v0.1` from the
separate worktree using the preserved old inputs. Its roles will not remove
newer units automatically. Retain certificate storage privately if needed.

## GitHub Actions

Validation CI uses disposable runners. The **Deploy relay** workflow is manual,
restricted to `main`, and does not automatically deploy pushes or PRs. Create the
`production` environment with appropriate reviewers/branch restrictions before
adding credentials; the workflow cannot create those protection rules.

Required environment secrets:

| Secret | Contents |
| --- | --- |
| `DEPLOY_PRIVATE_KEY` | Separate administrative deployment SSH key |
| `DEPLOY_KNOWN_HOSTS` | Independently verified relay host keys |
| `DEPLOY_INVENTORY` | Private inventory YAML |
| `TUNNEL_SERVICES` | Flat service configuration YAML |
| `TUNNEL_SECRETS` | Generated secret variables plus optional OIDC provider data |

The deployment account requires root/passwordless sudo. Inputs are masked and
written under the runner's private temporary directory, then removed in an
`always()` cleanup step. Abrupt termination can bypass cleanup; prefer ephemeral
runners. Never expose privileged self-hosted runners to untrusted PRs. Deploy
WSL clients locally; GitHub-hosted runners cannot reach private local WSL.

Do not use `--diff`, verbose inventory dumps or unredacted logs around private
inputs. `--syntax-check` does not contact hosts; fresh-host check mode cannot
fully validate configurations that require not-yet-installed binaries.
