# Deployment

## Supported setup

The controller runs on Linux/WSL with Python 3.11+ and pinned `ansible-core` from
`requirements-dev.txt`. Managed hosts use Ubuntu 24.04+ with systemd, OpenSSH,
amd64 or arm64. Native Windows FRP services and other Linux distributions are
outside the tested deployment scope. Enable WSL systemd according to
[Microsoft's instructions](https://learn.microsoft.com/en-us/windows/wsl/systemd)
before deploying; these roles do not shut down WSL or edit its boot configuration.

The administrator needs sudo/root on the relay and client. The client SSH account
must already exist, have a usable shell, and pass local PAM account policy. The
backend SSH port and local forwarding port must be unused. The relay public tunnel
port and FRP control port must be unused. Project-managed listening ports must be
1024 or higher so FRP needs no bind capabilities.

The relay's existing SSH daemon must permit public-key authentication and local
TCP forwarding for `frp-relay`. An existing `AllowUsers`, `AllowGroups`,
`DisableForwarding`, custom `AuthorizedKeysFile`, non-PAM locked-account policy,
or a `Match` block can prevent this. The main SSH configuration must include
`/etc/ssh/sshd_config.d/*.conf`, as the standard Ubuntu configuration does. Adjust
conflicting administrator policy deliberately through your normal process.

The generated relay key has `restrict,port-forwarding` and one `permitopen` target.
Its account has a `nologin` shell and locked password. It cannot run an ordinary
shell or commands. Local TCP forwarding can reach only loopback FRP on the configured
port. The role installs `/etc/ssh/sshd_config.d/00-frp-tunnel.conf` with an account-specific
`Match User` rule that blocks remote TCP and Unix socket forwarding, validates the
complete SSH configuration and effective account policy, and reloads SSH. Other
accounts keep their existing policy. Preserve an administration session when deploying.
OpenSSH's [`authorized_keys` options](https://man.openbsd.org/sshd.8#AUTHORIZED_KEYS_FILE_FORMAT)
describe these restrictions.

## Configuration

Keep your inventory and `secrets.yml` outside every checkout. Pass the same token
and relay key pair to both deployments. The generator produces separate login
and relay keys; only the relay private key is installed on the managed client.

| Variable | Default | Purpose |
| --- | --- | --- |
| `frp_server_address` | required | Relay IPv4 address or DNS name |
| `frp_login_user` | required | Existing non-root client account |
| `frp_public_ssh_port` | 2222 | Public relay SSH tunnel endpoint |
| `frp_backend_ssh_port` | 2222 | Dedicated SSH listener on client loopback |
| `frp_server_port` | 7000 | FRP control endpoint on relay loopback |
| `frp_forward_port` | 17001 | Client loopback SSH forwarding endpoint |
| `frp_relay_ssh_port` | 22 | Existing relay administration/SSH port |
| `frp_service_user` | frp-tunnel | Unprivileged FRP/transport account |
| `frp_relay_user` | frp-relay | Restricted forwarding account on relay |
| `frp_version` | 0.71.0 | Pinned upstream release |
| `frp_checksums` | pinned map | SHA256 per supported version/architecture |

Secret variables: `frp_token`, `frp_relay_private_key`,
`frp_relay_public_key`, `frp_login_public_key`, and `frp_relay_known_hosts`.
Public keys and host pins are handled privately as they can identify a deployment.

The shared token is 32 random bytes encoded as 64 hex characters. Services read it
from a restricted file rather than command-line arguments. All generated service
configuration is under `/etc/frp-tunnel`, binaries under
`/usr/local/lib/frp-tunnel/<version>`, and cached archives under
`/var/cache/frp-tunnel`. Secret copy tasks suppress Ansible logs and diffs.

No persistent transport runs through an HTTP/SOCKS proxy. Systemd clears both
uppercase and lowercase proxy variables; OpenSSH ignores user configuration and
explicitly disables `ProxyCommand` and `ProxyJump`. FRP is bound to loopback and
its outer SSH transport provides encryption, integrity, and pinned host identity.
Inner FRP TLS is disabled because that traffic stays inside the authenticated
SSH forwarding channel. Never expose the FRP control port publicly in this mode.

## Existing deployments and upgrades

Checking out or testing this project does not migrate an existing tunnel.
Project services use a separate `frp-tunnel-*` namespace, and a dedicated SSH
instance leaves other SSH daemon configurations intact. Choose unused ports for
a parallel deployment; otherwise stop the old conflicting listener in a planned
maintenance window. The roles refuse to adopt an unmarked `/etc/frp-tunnel`.
Fresh deployments also require unused dedicated account names; existing system
accounts are not repurposed. Managed account names cannot be changed in place.

Reapplying identical inputs is idempotent. Changes validate FRP and SSH
configuration before installation/restarts; failures do not replace a validated
configuration file. Upgrades retain older versioned binary directories. To roll
back, restore the previous release/checksum and compatible private inputs, then
redeploy. There is no automatic cross-host transaction: keep the previous private
inputs securely and schedule token/key rotations on both hosts together.

To add a release, update the checksum map after checking the upstream release and
its SHA256 digests through trusted sources, run CI, and deploy deliberately.
Architecture and version combinations absent from the pinned map are rejected.
The checksum protects against substituted archives, not a malicious upstream
release or a compromised source of the checksum itself.

`--syntax-check` validates playbook structure without contacting hosts. Ansible
check mode on a fresh host cannot fully validate configurations requiring binaries
and runtime directories that have not yet been installed. Do not pass `--diff`
while handling private deployment inputs; sensitive tasks suppress their own
diffs, but custom inventory and added tasks still need review.

## GitHub Actions

The repository includes validation CI and a separate manual relay workflow. It
does not deploy production on push or pull requests. All third-party Actions are
pinned to commit SHAs. Dependencies and FRP releases have explicit versions.

Create the `production` environment, restrict it to the protected `main` branch,
and configure any required reviewer rules before adding credentials. The workflow
references the environment but cannot create your protection rules. GitHub plan
and repository visibility affect available environment protections; see
[GitHub's environment documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).

Set these **environment secrets**, not committed workflow variables:

| Secret | Contents |
| --- | --- |
| `DEPLOY_PRIVATE_KEY` | Dedicated administrative deployment SSH private key |
| `DEPLOY_KNOWN_HOSTS` | Verified relay SSH host keys matching inventory endpoint |
| `DEPLOY_INVENTORY` | Private inventory YAML, including hosts and usernames |
| `TUNNEL_SECRETS` | Private generated `secrets.yml` contents |

The deployment key is distinct from the restricted automatic relay key. It needs
privilege to provision the server. The provided workflow assumes root or
passwordless sudo; password-based privilege escalation is not configured there.
Prefer a narrowly scoped deployment account and configure sudo deliberately.

The workflow masks individual private inventory/secrets values before invoking
Ansible, writes mode-600 files below `$RUNNER_TEMP`, and deletes that directory in
an `always()` cleanup step. It does not upload deployment artifacts. Abrupt runner
termination can bypass cleanup; hosted runners are ephemeral, while a self-hosted
runner requires its own cleanup and access controls. Avoid verbose/debug Ansible
output and do not attach unredacted logs to public issues.

Use the **Deploy relay** workflow from `main` after private input setup. Client
deployment remains local inside the intended WSL distribution. Never run a
public-PR workflow on a privileged self-hosted client runner with production keys.
