# Security

Do not publish live endpoints, identities, passwords, tokens, key material, OAuth
callback URLs or private deployment logs in issues. Use GitHub's **Report a
vulnerability** private advisory flow when enabled; otherwise obtain a private
maintainer channel before sharing details.

## Trust boundaries

- The origin-to-relay channel is an authenticated, independently host-pinned
  outbound SSH connection. The relay account has one permitted loopback forwarding
  destination, no shell, and no remote/Unix-socket forwarding.
- FRP control, service and authenticated status APIs are loopback-only. Explicit
  systemd socket proxies publish TCP services; Caddy alone publishes HTTP services.
  A web service must never acquire a public raw port that bypasses access policy.
- Public TCP forwarding does not authenticate or encrypt its application protocol.
  SSH provides its own encryption/key authentication; other applications must
  supply their own controls. Do not casually expose databases or admin ports.
- Caddy terminates public HTTPS. A relay administrator can read/modify web traffic;
  this is not browser-to-origin end-to-end encryption. HTTPS backend connections
  verify chain and hostname using normal OS trust. A plain HTTP LAN backend hop
  is not protected by the origin-to-relay SSH channel.
- Public/application/Basic/OIDC policies are explicit per web application. Incoming
  spoofable identity and forwarding headers are removed. Application mode leaves
  authentication to the backend; Basic and OIDC are not blanket authorization for
  every action inside an application.
- OIDC uses a real external provider, exact per-host callbacks, verified email,
  nonce and S256 PKCE. Email/domain allowlists use OR semantics. Sessions use
  host-only secure cookies and can remain valid for up to one hour after provider
  account revocation. There is no cross-application logout or immediate revocation
  service. OIDC-protected hosts reserve `/oauth2/`.

## Credentials and host security

Keep administration, relay and login keys separate. The reconnecting client
retains its restricted relay private key with mode 600. Configurations, provider
secrets, certificate keys, input files and field-test artifacts belong outside
public checkouts/logs. Basic passwords are stored as bcrypt hashes, not plaintext;
hashes and public keys still identify deployments and should remain private.

The optional managed SSH service accepts the configured public key, denies
passwords/keyboard-interactive/root login, and does not replace native host SSH.
Turning it off removes only its owned configuration and unit. Do not disable host
verification or enable password authentication to work around policy failures.

A compromised controller, root account on either host, privileged workflow, or
trusted origin administrator can compromise the deployment. Multiple services
share an FRP identity and relay; this is not secure multi-tenant isolation. Keep
all publicly exposed software patched and protect the relay with appropriate
firewall/provider controls. The project does not provide global DDoS protection,
a WAF, CDN, high availability, or an identity provider.

Pinned FRP, Caddy and oauth2-proxy downloads use checked SHA256 digests. Checksums
protect against substituted downloads, not a malicious upstream release or a
compromised checksum source. Review version/hash changes and run the real field
suite. The suite uses disposable credentials and a test IdP; never run it on a
production or shared workstation/server.

Network filtering is path-dependent. No guarantee is made of bypassing every
filter or preserving sessions across sleep, reconfiguration or outages.
