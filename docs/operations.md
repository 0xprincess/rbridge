# Operations

## Discover and manage publications

Use the workspace front door from the controller:

```bash
rbridge list
rbridge list --show-endpoints
rbridge status --json --timeout 3
rbridge plan
rbridge remove demo --dry-run # preview only
rbridge remove demo           # deploy removal
```

`list` never probes: JSON reports `checked: false`, `ok: null` and
`health: not_checked`. `status` checks the configured public ingress from this
network and reports its scope, not a complete application health claim.
`--local-role client|server` adds the existing layered doctor checks for a role
installed on the machine running the command; it needs permission to read that
host's FRP configuration. For privileged diagnostics without changing workspace
ownership, use the standalone `sudo ... tunnel.py doctor` commands below.

Service numbers follow current configuration order. Names, endpoints and
connection commands appear only with `--show-endpoints`; do not share that
output indiscriminately. SSH instructions require the correct private login key
and an independently verified backend host pin. Generic TCP endpoints do not
imply SSH. Basic challenges/OIDC redirects do not prove a completed login.

Bridge exit codes are 0 for successful actions/previews/listing or passing scoped
checks, 1 for failed/unknown checks or failed Ansible deployment, 2 for input,
workspace or subprocess errors, and 130 for interruption. `--json` writes one
result to stdout; apply interruption previews and progress go to stderr and
remain redacted unless endpoint disclosure was requested.

### Impact and failed applies

Plans compare private input against `last-applied.json`, the last successful
**CLI** application, not actual hosts. Manual Ansible changes and external drift
are not tracked. Unknown baselines do not invent a live diff. Historical removal
indexes in plans refer to the previous apply, not today's `remove NUMBER`.

A manifest, access-policy or FRP-token change can revoke every registration and
disconnect unchanged services. Infrastructure changes also warn about restart
risk. Even unchanged input is not a no-downtime promise. If inventory targets
change, inspect the former hosts: applying new targets does not uninstall them.

After failure/interruption, `apply-pending.json` marks the workspace incomplete.
Inspect private `apply.log`, diagnose both hosts, correct the inputs and run
`plan`, then `apply`. A successful rollout clears the marker; do not delete it
to claim recovery. Saved desired inputs remain available for retry.

Exiting the CLI does not stop systemd publications. Backends must keep running
and the origin machine awake; under WSL the distribution must also remain
running and Windows awake. Removing a publication does not stop
the application. Removing the last HTTP publication closes managed web ingress;
removing all publications leaves idle transport infrastructure, accounts,
credentials and certificate storage. [Full removal](#rotation-and-removal)
remains an explicit administrative operation.

## Diagnose the installed deployment

Run from a repository checkout on the client or relay. Roles install Python's
YAML dependency; use Python 3.11+ and permission to read private FRP configuration.
The CLI does not install a second copy of its model on managed hosts.

```bash
python3 scripts/tunnel.py validate \
  --config "$PRIVATE/services.yml" --config "$PRIVATE/secrets.yml"
sudo python3 scripts/tunnel.py doctor \
  --config "$PRIVATE/services.yml" --role client --json
sudo python3 scripts/tunnel.py doctor \
  --config "$PRIVATE/services.yml" --role server --json
```

Choose the role present on that host. If services use OIDC, also pass
`--config "$PRIVATE/secrets.yml"` so the full model can be validated. Files are
flat YAML variable mappings, not Ansible inventories. Generated `services.yml`
includes `frp_server_address`; it is needed for public TCP checks.

Doctor reads admin credentials from `/etc/rbridge/frpc.toml` or `frps.toml`.
`--frp-config FILE` selects another installed configuration. If the default file
is absent, flat configuration may supply the admin variables instead. API
requests carrying credentials are restricted to literal loopback destinations.
Never expose either admin API publicly.

### Results and limits

Exit codes: **0** healthy/valid, **1** failed or unknown diagnostic state,
**2** invalid configuration. JSON includes `ok`, `role`, and `checks` with
`stage`, `status`, `message`, `action` and optional one-based `service` index.
The index follows configuration order; endpoints and credential values are not
printed. Check stages:

| Stage | Evidence and interpretation |
| --- | --- |
| `transport` | Client systemd state and registered-proxy evidence; a process or open control port alone is insufficient |
| `registration` | Authenticated FRP API reports the configured proxy running/online |
| `backend` | Client checks the actual backend; relay checks traverse its loopback FRP endpoint |
| `public_ingress` | Public TCP connection or hostname-specific HTTPS response with normal TLS verification |

A client-side or public TCP `ok` means only a handshake, not application
readiness or a complete transfer. A relay socket proxy can accept a connection
while its backend is unavailable. Server-side TCP backend checks therefore
report `unknown` even when the relay listener accepts: a deployment containing
generic TCP services can legitimately return exit 1 on the server. Use the
client-side doctor and a real protocol client to establish backend health.
HTTP checks require a real response: a dead backend or broken TLS does not pass
merely because a listener accepted TCP.

Basic ingress checks expect a 401 challenge; OIDC expects a login redirect.
These checks do not submit credentials, follow redirects, or prove browser
login/token exchange. Test that separately. Application-owned authentication may
legitimately respond 401/403; public HTTP ingress requires a 2xx/3xx response.
Doctor issues read-only status requests and bounded
TCP/HTTP GET probes; `--timeout` controls each probe's wall-clock deadline,
including DNS. Public checks originate from the machine running doctor, so
repeat from an external client network to detect ingress firewall/filtering issues.

## Native SSH access on filtered networks

The public client-to-relay connection and the origin-to-relay SSH transport are
different network legs. Successful login, `doctor`, or a transfer between VPSs
does not establish that bulk traffic works from the workstation.

v2rayN in **normal system-proxy mode, not TUN**, does not automatically route
OpenSSH. Browsers may use the proxy while `ssh` remains direct. Choose the
route explicitly in a private OpenSSH profile; do not change global routing,
weaken host-key checking, or silently fall back to direct access.

Example `/private/tunnel/client.conf` (replace the host, user, port and paths):

```sshconfig
Host tunnel-direct
    ProxyCommand none

Host tunnel-proxied
    ProxyCommand /usr/bin/nc -X 5 -x 127.0.0.1:10808 %h %p

Host tunnel-direct tunnel-proxied
    HostName relay.example.invalid
    Port 2222
    User developer
    IdentityFile /private/tunnel/login_id_ed25519
    UserKnownHostsFile /private/tunnel/login_known_hosts
    GlobalKnownHostsFile /dev/null
    StrictHostKeyChecking yes
    IdentityAgent none
    IdentitiesOnly yes
    ProxyJump none
    ConnectTimeout 10
    ServerAliveInterval 15
    ServerAliveCountMax 3
```

The login host pin must be the **origin SSH server's** independently verified
key, indexed under the relay's published address and port—not the relay's
administrative host key. The profile and keys stay outside the checkout.
The SOCKS example requires OpenBSD netcat (`netcat-openbsd` on Debian, Ubuntu
and openSUSE; `openbsd-netcat` on Arch) and a reachable local SOCKS5 listener.
Fedora/RHEL's `nc` is Ncat: use `ProxyCommand /usr/bin/ncat --proxy
127.0.0.1:10808 --proxy-type socks5 %h %p` instead.
`10808` is an example, not an auto-detected
setting. Mirrored WSL can reach a Windows loopback listener; verify reachability
instead of assuming NAT-mode WSL uses the same address. Do not expose an
unauthenticated SOCKS listener to an untrusted network.

Use the selected profile for ordinary SSH, SCP and SFTP:

```bash
ssh -F /private/tunnel/client.conf tunnel-proxied
scp -F /private/tunnel/client.conf ./file tunnel-proxied:destination
sftp -F /private/tunnel/client.conf tunnel-proxied
```

Check that exact route from the workstation, rather than substituting a VPS:

```bash
python3 scripts/tunnel.py bulk-check \
  --ssh-config /private/tunnel/client.conf --host tunnel-proxied \
  --size-mib 16 --timeout 60 --json
```

`bulk-check` authenticates and executes bounded, file-free transfer commands.
The backend needs `sha256sum` and Python 3, already present in the supported
deployment. Both directions carry random data and verify length/SHA-256.
Each uses a fresh SSH connection without compression or connection reuse.
Failures are not retried or replaced with a successful handshake. JSON reports
`upload` and `download` checks, elapsed seconds, and verified byte counts only
on success. Exit codes are 0 for both directions verified, 1 for any failure,
and 2 for invalid arguments. The deadline also terminates SSH proxy descendants.

For diagnosis, compare the same command with `--host tunnel-direct`. A proxy
success and direct failure localize a route-dependent problem; they do not,
alone, identify a DPI product or rule. Capture TCP retransmissions/ACKs and
backend byte counts privately to distinguish network blackholing from a
backend waiting for EOF. Increasing timeouts, disabling host checks, or changing
SSH QoS blindly is not a repair.

On Windows, also compare native Windows OpenSSH with WSL against the same
endpoint. If only WSL stalls, keep that distinction in the diagnosis rather
than declaring the relay port generally blocked. A successful native Windows
transfer does not establish that the WSL path works. Neither changing SSH QoS
nor lowering MSS should be treated as a fix without repeating the byte checks.

This profile changes **public native SSH access only**. The managed
`rbridge-transport` and administrative Ansible connections remain direct;
do not assume this profile reroutes either one.

## Services and logs

On the relay:

```bash
sudo systemctl status rbridge-server rbridge-web --no-pager
sudo systemctl list-units 'rbridge-publish-*' 'rbridge-oidc-*'
sudo journalctl -u rbridge-server -u rbridge-web \
  -u 'rbridge-oidc-*' --since '-10 min' --no-pager
sudo ss -ltnp
```

`rbridge-web` exists only with web publications. All FRP proxy/control/admin
and OIDC listeners must be loopback. Public TCP sockets can remain listening
while the origin is disconnected; their presence no longer proves registration.

On the client:

```bash
sudo systemctl status rbridge-transport rbridge-client --no-pager
sudo journalctl -u rbridge-transport -u rbridge-client \
  -u rbridge-sshd --since '-10 min' --no-pager
sudo ss -tnp
```

The managed `rbridge-sshd` unit exists only with `frp_managed_ssh: true`.
Native host SSH is separate. The persistent outbound transport goes directly to
the relay's SSH port; FRP connects to client loopback. Systemd clears proxy
environment variables, and OpenSSH ignores user proxy configuration. Logs can
contain endpoints, identities, OAuth URLs and filesystem paths; review privately.

## WSL startup

From Windows PowerShell:

```powershell
Get-ScheduledTask -TaskName 'rbridge - Ubuntu-24.04'
Start-ScheduledTask -TaskName 'rbridge - Ubuntu-24.04'
```

Adjust the suffix for your distribution. Startup occurs at user logon, not boot
before logon. Windows must be awake. The keepalive uses an exclusive lock to
avoid duplicate Linux keepalive processes. After deliberate `wsl --shutdown`,
restart the task or open WSL; systemd alone does not keep a distribution alive.

## Reconnection and configuration changes

```bash
sudo systemctl restart rbridge-transport
```

SSH keepalives detect lost connections, systemd restarts transport, and FRP
retries registration. Starting transport also starts its FRP client, including
after an explicit stop/start maintenance cycle. Existing connections can disconnect during any restart,
computer sleep or network outage. Reconnect after registration recovers.

Use `rbridge publish/remove ...` (real deployment by default; add `--dry-run`
for a non-mutating preview), or edit private services and run
`rbridge plan` followed by `rbridge apply`. Direct Ansible operators must validate
and deploy both hosts using the same files.
Removing a service cleans its generated public sockets and proxy configuration.
Changing OIDC policy/ports can briefly return an error while authenticators
restart; requests do not bypass authentication. Removing the final HTTP service
stops web ingress and removes its generated configuration/authenticators, while
retaining the managed account and certificate storage. Setting managed SSH false
removes its dedicated service/configuration/key but never native SSH.

For planned maintenance, stop/disable the Windows keepalive task first. Stop the
FRP client before its transport; stop managed SSH only if it exists. Systemd
dependencies or keepalive can restart individual units if you leave the rest running.

## Troubleshooting

| Symptom | Checks |
| --- | --- |
| Relay SSH authentication fails | Independently verified host pin, relay key, account/PAM policy, forwarding restrictions |
| Local forward cannot bind | Client-side port collision |
| Registration fails | Matching tokens, permitted relay ports, FRP status API and logs |
| Public TCP accepts but app fails | Actual client backend, registration and protocol-level transfer; not just listener state |
| Certificate issuance fails | DNS, public 80/443, ACME reachability/rate limits and Caddy logs |
| HTTPS backend returns 502 | Backend availability, relay OS CA trust and backend URL hostname matching certificate |
| OIDC discovery/readiness fails | HTTPS issuer accessibility/trust and provider configuration; bootstrap a tunneled issuer first |
| OIDC login denied | Exact callback, client secret, verified email, configured email/domain restrictions, PKCE support |
| Transfer stalls only on one network | Run `bulk-check` from that workstation with explicit direct/proxied profiles; compare TCP loss and backend byte counts, not just login or VPS-to-VPS results |
| Host fingerprint changes | Verify independently before updating the pin; never disable host checking |
| WSL exits | Windows task state, correct distribution, enabled systemd and installed keepalive |

## Rotation and removal

Keep administration, login and relay keys separate. Rotate the login public key
in private inputs. Rotate relay keys, FRP tokens and admin passwords on both
hosts in a maintenance window. Preserve existing administrative access. Rotate
OIDC client/cookie secrets deliberately; cookie-key rotation invalidates sessions.

Under WSL, to unregister Windows startup:

```powershell
./scripts/register-wsl-startup.ps1 -Distribution Ubuntu-24.04 -Remove
```

For full uninstall, disable all owned `rbridge-*` units on the relevant hosts.
Review ownership before removing generated unit/configuration files, the
keepalive helper, versioned binaries, caches and dedicated accounts. Certificate
storage contains private keys; retain or delete it deliberately. Remove only the
project's account-scoped SSH policy, validate native SSH configuration and reload
SSH (`ssh.service` on Debian/Ubuntu, `sshd.service` elsewhere). On SELinux hosts,
optionally delete the local customizations the roles added:
`semanage fcontext -d '/usr/local/lib/rbridge(/.*)?'` and, for the managed SSH
port, `semanage port -d -t ssh_port_t -p tcp PORT`.
Run `systemctl daemon-reload` afterward. Full removal remains manual because
reused accounts/ports and retained credentials require review. See
[rollback](deployment.md#upgrades-and-rollback) before redeploying `v0.1`.
