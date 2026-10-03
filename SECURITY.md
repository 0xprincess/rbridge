# Security

Do not report vulnerabilities with live endpoints, tokens, key material, personal
paths, or private deployment logs in a public issue. When enabled for a repository,
use GitHub's **Report a vulnerability** private advisory flow. Otherwise obtain a
private reporting channel from the repository maintainers before sharing details.

The security boundary is the authenticated, host-pinned outbound SSH connection,
the restricted relay account, FRP token authentication, and the key-only backend
SSH instance. FRP's loopback control listener is not intended for public exposure.
The publicly exposed service is still an SSH server and must be kept patched.

Use fresh, separate login and relay keys. The automatic relay key is retained on
the client with mode 600 because unattended reconnection requires it; its server
account cannot open a shell and has one permitted forwarding destination. Secure
the client disk and privileged accounts. A compromised controller, client root
account, relay root account, or production workflow can compromise a deployment.

Both managed hosts require supported Ubuntu security updates. Only the pinned FRP
version is covered by the current CI configuration; verify newer versions and
checksum sources before deployment. The project does not promise to bypass every
network filter or provide uninterrupted sessions across outages.
