# Deployment

## Supported setup

The controller is any Linux system, including WSL2, with Python 3.11+, OpenSSH
and the pinned `ansible-core` in `requirements-dev.txt`.

Managed hosts (relay and origin) need systemd 247+ as the service manager,
OpenSSH, Python 3.8+ for Ansible, amd64/arm64, and one of these distribution
families. The roles select packages and the native SSH unit by Ansible's
`os_family`; other families fail validation before any change.

| Family | Examples | Dependencies installed with | Native SSH unit |
| --- | --- | --- | --- |
| Debian | Debian 12+, Ubuntu 22.04+ | `apt` (cache refreshed) | `ssh.service` |
| RedHat | Fedora, RHEL/AlmaLinux/Rocky 9+, CentOS Stream | `dnf` | `sshd.service` |
| Suse | openSUSE Tumbleweed, Leap 16 | `zypper` | `sshd.service` |
| Archlinux | Arch Linux | `pacman -S --needed` | `sshd.service` |

Arch does not support partial upgrades, so the roles never refresh pacman
databases: run `pacman -Syu` before applying. Hosts without systemd (Alpine,
Void, OpenRC Gentoo), immutable/declarative systems such as NixOS, and native
Windows services are out of scope.

On SELinux hosts (Fedora/RHEL family and recent openSUSE default to enforcing),
the roles install `policycoreutils-python-utils` and add two persistent local
policy customizations: `bin_t` for `/usr/local/lib/rbridge` and `ssh_port_t` for
the managed SSH backend port. Every rbridge unit then runs as
`unconfined_service_t`, still restricted by its systemd sandbox. The roles never
disable or otherwise relax SELinux enforcement. AppArmor needs no changes. Host
firewalls (`firewalld`, `ufw`, `nftables`) remain administrator-owned.

Under WSL, enable [WSL systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd)
first; these roles do not edit WSL boot configuration or shut down distributions.

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

The role installs an account-scoped `00-rbridge.conf` and a key with
`restrict,port-forwarding` and one `permitopen` destination: loopback FRP control.
The account has a locked password and `nologin` shell. Command execution, remote
forwarding and Unix socket forwarding are denied. The complete native SSH
configuration and effective account policy are checked before reload.

## Publishing workspace

The recommended front door is the installed `rbridge` command. Follow the README's
`init` instructions once, then use `publish`, `plan`, `apply`, `list`, `status`
and `remove`. Every command accepts `--private-dir`; otherwise it uses `$PRIVATE`
or `~/.config/rbridge-private`. Install with `sh install.sh` from a checkout;
the installed command bundles its own Python environment and Ansible roles and
works from any directory. Contributors may still run `python scripts/bridge.py`
in a source virtualenv. The workspace must be owned by the invoking user, outside
every checkout and on a filesystem that enforces Unix permissions (not a Windows
drive such as WSL `/mnt/c`, nor FAT/NTFS/SMB), with directory mode 0700 and file
mode 0600.

`init` generates only a transport identity: `frp_managed_ssh: false` and explicit
`frp_services: []`. It targets the current Linux machine (or WSL distribution)
as the origin and an existing relay via pinned SSH. Review `inventory.yml` for remote origins or
administrative/sudo differences. `controller.yml` stores the administrative key
path, not a second key. The administrative key must be usable without an SSH
password prompt; an encrypted key can be loaded into the user's SSH agent.
No cloud resources, DNS records, firewall rules or Windows startup task are
created by this CLI.

| Command | Effect |
| --- | --- |
| `init ...` | Create private web-only inputs; no remote changes |
| `publish PORT --name NAME --hostname HOST --access public` | Save a new local HTTP publication and deploy both hosts |
| `publish http://HOST:PORT ...` | Publish an explicit HTTP/HTTPS backend using the shared schema |
| `publish PORT --name NAME` | Deploy an app update; preserve its policy/hostname/relay port |
| `publish ... --dry-run` | Preview the candidate revision without changing files or hosts |
| `plan` | Compare current files with the last successful CLI apply, without contacting hosts |
| `apply [--dry-run]` | Deploy current private files, or preview without mutation with `--dry-run` |
| `list [--show-endpoints]` | Describe configured services; no health probes |
| `status [--timeout 3]` | Bounded public ingress checks from the controller's network |
| `remove NAME_OR_NUMBER [--dry-run]` | Remove and deploy by default, or preview with `--dry-run`; number is the current one-based list index |

Publishing and removal now execute by default rather than requiring an opt-in
deployment flag. Impact is still displayed before deployment. `--dry-run`
never saves the candidate revision, creates a lock/log/state file, prompts for
sudo, or runs Ansible; repeat without the flag to execute it. `init` remains
local workspace creation; use `apply` afterward to deploy idle infrastructure.

The default publication name is `web`; use explicit names for multiple apps.
New HTTP publications require a hostname and explicit access policy. The
allocator selects an unused configured port in 12000–65535, excluding control,
admin, web, TCP-publication and OIDC listeners. This checks the service model,
not unrelated live OS listeners; deployment may still fail on an external port
conflict. Updating a service retains its existing relay port.

`--access public` and `--access application` cover the simple cases.
`--access-file FILE` reads a private flat access mapping for any supported mode:

```yaml
mode: oidc
allowed_domains:
  - example.com
```

OIDC provider settings still belong in `secrets.yml`; register the exact callback
and meet the [provider requirements](#web-access-policies). An omitted OIDC
listener port is allocated automatically. Basic policy files contain `mode:
basic` and the existing `users` mapping of bcrypt hashes, never plaintext
passwords. Changing a policy requires an explicit option/file; omitted options
on an update never silently make an app public.

Keep service lists and `frp_managed_ssh` in `services.yml`, not a secrets overlay.
Applying stages one immutable variable/inventory revision for both hosts, forces
strict host-key verification against private `known_hosts`, disables implicit
SSH proxies, and uses the existing fail-closed Ansible playbook. Relay pins in
`secrets.yml` and `known_hosts` must match. Existing deployments can use this
workspace flow by supplying private `controller.yml` with `admin_key: /private/admin-key`;
without a saved CLI baseline, the first plan is explicitly unknown.

Concurrent CLI applies to one workspace are rejected. This is not a distributed
lock across controllers or a transaction across hosts. `--ask-become-pass` prompts
on a terminal and supplies the password only through a temporary private variables
file, removed after the attempt; otherwise use passwordless sudo. Ansible output
goes to private `apply.log`, never public/JSON stdout. A successful apply records
`last-applied.json`; failures retain the desired service configuration and
`apply-pending.json` so plain `apply` retries that revision without losing the
last successful baseline. Baselines and logs may contain credentials.

## Private configuration


Keep these outside every checkout:

- `inventory.yml`: managed hosts, administrative connection and sudo settings.
- `services.yml`: editable flat variables including `frp_services`,
  `frp_server_address`, `frp_managed_ssh` and the optional client login identity.
- `secrets.yml`: relay/login keys, trusted relay host pins, FRP/admin credentials
  and, when used, OIDC provider secrets.

For an advanced SSH-first workspace, generate the isolated managed login service:

```bash
python scripts/create-secrets.py --output "$PRIVATE" \
  --server-host relay.<relay-ip>.rbr.sh --client-user developer \
  --known-hosts "$HOME/.config/verified-relay-hosts"
```

This also creates separate relay and login keys. The non-root client login
account must already exist. Use `--services-file FILE` for an existing flat
service configuration and `--no-managed-ssh` to omit login-key generation.
The generator validates the shared model and refuses existing directories.
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

An explicit `frp_services: []` publishes nothing and supports infrastructure-only
bootstrap or final-service removal. It is distinct from an omitted service list.
With no publications, FRP's allowlist remains restricted to its already-bound
control port rather than using an unrestricted empty allowlist. Web-only
workspaces disable managed SSH; removing the last service through `bridge`
also disables the dedicated managed SSH backend, never the host's native SSH.

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
add `rbridge-publish-NAME.socket` and `.service`, using systemd-socket-proxyd.
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
| `frp_service_user` | rbridge | FRP/transport account |
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

The installed FRP authentication token is derived with HMAC-SHA256 from the
private base `frp_token` and the canonical service manifest (including access
policies and, when used, OIDC provider configuration). It is not the base value
itself. Both hosts must receive the same revision. Changing a mapping or policy
revokes old registrations and connections when the relay restarts, preventing
an old protected backend from appearing behind a newly public route during a
partial deployment. A relay-only cutover fails closed until the client is updated.

Before installing a changed revision credential, the relay stops its owned FRP
and web services. A failed relay apply aborts the automatic client rollout; even
a subsequent client-only apply cannot serve new registrations under the old web
access policy. Correct the relay failure and apply both hosts to restore ingress.

## HTTPS and DNS

Every published hostname must resolve to the relay, using either:

- **`rbr.sh` DNS:** `rbr.sh` is a nip.io alias, so `<name>.<relay-ip>.rbr.sh`
  resolves to the relay IPv4 address embedded in the name without DNS records.
  The examples in these docs use this form.
- **Your own domain:** point an A record such as `app.example.com` at the relay.
  Configure only DNS you control and can validate.

Permit inbound TCP 80/443 and allow outbound DNS/ACME traffic. Caddy obtains and
renews certificates automatically; HTTP redirects to HTTPS. Wildcards and
DNS-provider API automation are not implemented.

The dedicated `rbridge-web` unit uses its own pinned Caddy binary and
`/etc/rbridge/web/Caddyfile`; it does not import/adopt `/etc/caddy` or replace
an unrelated Caddy service. An existing listener on 80/443 must be handled by the
administrator. Certificates persist in `/var/lib/rbridge-web`, including when
the final web publication is removed, so later re-enablement retains state.

HTTPS terminates at the relay. The relay can read HTTP traffic; it is not an
end-to-end encrypted web service from browser to origin. HTTPS backends still
receive certificate-chain and hostname verification using the `backend` host as
TLS identity. Install a private backend CA in the relay's normal OS trust store
when appropriate, then restart `rbridge-web` to reload cached system roots;
a configuration reload alone is insufficient after CA rotation. There is no
insecure-verification switch. The application
receives its public hostname as `Host`. WebSockets and streaming are supported.

`frp_web_tls: internal` uses Caddy's local CA, not a browser-trusted public
certificate. Distribute and trust its root deliberately; the role does not
silently change system/browser trust. Its root is at
`/var/lib/rbridge-web/data/caddy/pki/authorities/local/root.crt`.

## Web access policies

Each HTTP service must select exactly one mode:

- `access: {mode: public}`: anyone can reach the backend. Choose deliberately.
- `access: {mode: application}`: the backend owns authentication. Authorization
  headers are preserved. The tunnel does not implement or verify your app's login.
- `mode: basic`: add `access.users`, a mapping of usernames to bcrypt hashes.
  Generate hashes privately with Caddy's interactive `hash-password --algorithm
  bcrypt` command (pinned binary under `/usr/local/lib/rbridge/caddy-2.11.7`).
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
enabling apps that depend on it. Full deployment checks OIDC readiness after
the client rollout. With `--limit frp_servers`, readiness may fail until the
client receives the matching revision; apply both sides rather than disabling
the readiness or authentication checks.

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
validated before replacement. Manifest/policy or FRP-token changes revoke all tunnel
registrations and can disconnect unrelated services sharing this tunnel.

The tag preserves source, not a snapshot of installed machines. To
inspect/redeploy the baseline:

```bash
git worktree add --detach ../rbridge-v0.1 v0.1
```

Before running the old roles, stop and disable every `rbridge-*` unit on both
hosts; `v0.1` installs differently named units that would otherwise conflict on
the same listeners. Review installed units and act only on this project's owned
resources. Remove their owned unit files and run `systemctl daemon-reload` so a
later reboot cannot restore conflicting listeners. Redeploy `v0.1` from the
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
WSL or other private origins locally; GitHub-hosted runners cannot reach them.

Do not use `--diff`, verbose inventory dumps or unredacted logs around private
inputs. `--syntax-check` does not contact hosts; fresh-host check mode cannot
fully validate configurations that require not-yet-installed binaries.
