# Operations

## Health and startup

On the relay:

```bash
sudo systemctl status frp-tunnel-server --no-pager
sudo ss -ltnp
```

The FRP control listener must be on `127.0.0.1`; the configured public SSH port
appears only after client registration. On the client:

```bash
sudo systemctl status frp-tunnel-sshd frp-tunnel-transport frp-tunnel-client --no-pager
sudo journalctl -u frp-tunnel-transport -u frp-tunnel-client -n 30 --no-pager
sudo ss -tnp
```

The outbound network connection should be the SSH transport to the relay SSH
port. FRP itself connects to client loopback. Logs and status output may disclose
endpoints, usernames, fingerprints, and filesystem paths: review them privately.

For WSL, check the registered task in Windows:

```powershell
Get-ScheduledTask -TaskName 'FRP tunnel - Ubuntu-24.04'
Start-ScheduledTask -TaskName 'FRP tunnel - Ubuntu-24.04'
```

Adjust the task suffix to your distribution. The keepalive uses an exclusive lock
to prevent duplicate Linux keepalive processes. Startup occurs at user logon.
It requires Windows to be awake and the configured distribution to be available.

## Reconnection and maintenance

The SSH transport detects a lost connection with keepalives and systemd restarts
it. FRP retries login rather than exiting on the initial failure. Restarting the
transport propagates to its dependent client service:

```bash
sudo systemctl restart frp-tunnel-transport
```

During an outage or restart, active SSH sessions can disconnect and the public
listener can temporarily disappear. Reconnect after the client registers again.
No tunnel can preserve a session across every computer sleep or network outage.

For maintenance, first stop the Windows keepalive task, disable startup if needed,
then stop the client units. Stop the FRP client before its transport:

```bash
sudo systemctl stop frp-tunnel-client frp-tunnel-transport frp-tunnel-sshd
```

Restart the task or the client services after maintenance. Stopping only one
service can cause dependencies or the keepalive helper to start it again.

## Troubleshooting

| Symptom | Checks |
| --- | --- |
| Relay SSH authentication fails | Host pin, dedicated key, relay account SSH policy, forwarding permissions |
| Local forwarding cannot bind | Forwarding port collision; choose an unused port in private inventory |
| Backend cannot start | Existing SSH listener on backend port; valid user and host key; `/run/sshd` exists |
| Public port missing | FRP client registration, token agreement, server port whitelist, backend listener |
| Login succeeds but transfer stalls | Test larger data; examine transport and network filtering privately |
| Host key changed | Verify the change independently before replacing the stored host pin |
| WSL stops after logon | Task status, correct distribution, systemd enabled, keepalive helper installed |

The relay system SSH policy may deny a locked `nologin` account on distributions
or policies outside the tested Ubuntu/PAM setup. Do not disable host checking or
enable password login as a shortcut; inspect the specific policy.

## Rotation and removal

Keep login, relay, and deployment administration keys separate. Replace the
login public key in private inputs to rotate access to the exposed client.
For relay key or FRP token rotation, redeploy both hosts using the same new private
inputs in a maintenance window. Preserve your admin connection for recovery.

To remove this project, stop/remove the Windows startup task:

```powershell
./scripts/register-wsl-startup.ps1 -Distribution Ubuntu-24.04 -Remove
```

Then disable the project units on the client and server. Remove only the
`frp-tunnel-*` unit files, `/usr/local/sbin/frp-tunnel-keepalive`, project-owned
configuration, versioned binaries, cache directories, and the dedicated accounts
after checking ownership. Remove the account-specific
`/etc/ssh/sshd_config.d/00-frp-tunnel.conf`, validate SSH configuration, and reload
the system SSH service. Run `systemctl daemon-reload` afterward. Removal is a
deliberate manual operation because account/port reuse and retained keys deserve
review; the roles never recursively delete deployment directories.
