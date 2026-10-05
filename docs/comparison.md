# Cloudflare Tunnel and Tailscale comparison

Measured **2026-10-05** using project revision
[`2ca728a`](https://github.com/0xprincess/frp-tunnel/commit/2ca728a8c3a489368cba406fbc86a1e66a00e38d)
and **cloudflared 2026.9.3**. FRP and Cloudflare **Quick Tunnel** were exercised
against the same backends from the same visitor on the same temporary VPS pair.
Tailscale was **not** installed; its published results are separate below.

**These are real measurements, not universal speed ratings.** Quick Tunnel is
not a named production tunnel. The visitor ran on the FRP relay, so FRP ingress
was local while Cloudflare traversed its edge network. This measures those
specific paths, not equal-distance transports or typical Internet-user latency.

[Sanitized evidence](benchmark-results/2026-10-05-quick-tunnel.json) contains
18 TCP trials, 1,500 timed echo samples, 200 HTTPS requests, 12 verified HTTPS
bulk transfers, 12 verified SSH transfers, 600 resource samples, ten interruption
trials and one Quick Tunnel process-restart check. Timed transfers completed
without failed records. Interruption-induced failures are recorded, not hidden.
Addresses, assigned hostnames, identities, keys and private logs are excluded.

## Matched transport measurements

Paths, with a separate origin VM in every case:

- **Direct control:** visitor on relay → origin's direct fixture listener.
- **FRP:** same visitor → relay-local public socket → FRPS → persistent outbound
  OpenSSH transport → FRPC → origin backend. HTTPS additionally uses Caddy.
- **Quick Tunnel:** same visitor → client `cloudflared` for TCP/SSH, or ordinary
  HTTPS → Cloudflare edge → origin `cloudflared` → the **same backend**.
  Its public TCP/SSH path is TLS WebSocket transport, not a native public socket.

TCP throughput is receiver goodput in decimal Mbit/s: one stream, Cubic,
30 timed seconds after three omitted warm-up seconds, three trials per direction
and path. Each repetition interleaved direct/FRP/Quick upload, then their
reverse-direction downloads. Cells show **median [minimum–maximum]**.

| Metric | Direct control | FRP tunnel | Cloudflare Quick Tunnel |
| --- | --- | --- | --- |
| TCP upload, Mbit/s | **5,585.39 [5,577.40–6,288.68]** | **2,479.09 [2,428.59–2,486.00]** | **292.08 [281.56–303.41]** |
| TCP download, Mbit/s | **8,862.02 [8,749.45–8,924.36]** | **2,306.14 [2,246.54–2,322.56]** | **725.65 [704.85–762.18]** |
| Persistent 1 KiB echo, p50 / p95, ms | **0.511 / 0.742** | **1.362 / 1.918** | **29.614 / 30.332** |

Quick Tunnel's TCP and SSH behavior was verified with actual payloads, not
inferred from a listening port or connector registration. Each echo path had
500 checked responses after 20 excluded warm-ups, with a 10 ms pause between
exchanges. No timed samples were discarded. These values exclude TCP setup.

### HTTPS and native SSH

HTTPS used fresh HTTP/1.1/TLS connections, verified certificates, unique query
nonces and an origin `Cache-Control: no-store` response. All 100 Quick HTTPS
latency responses reported **`CF-Cache-Status: DYNAMIC`** and visitor edge
**`CDG`**. They were not CDN cache-hit measurements. Both products returned the
exact same **1,088-byte** body in all 100 requests each.

| Metric | FRP tunnel | Cloudflare Quick Tunnel |
| --- | --- | --- |
| Fresh HTTPS time to first byte, p50 / p95, ms | **6.262 / 9.601** | **124.583 / 158.739** |
| Fresh HTTPS total time, p50 / p95, ms | **6.386 / 9.785** | **124.821 / 158.906** |
| Verified 8 MiB HTTPS upload / download, median effective Mbit/s | **689.38 / 1,194.60** | **160.20 / 300.08** |
| Verified 64 MiB SSH upload / download, median effective Mbit/s | **651.54 / 521.74** | **211.37 / 300.60** |

Bulk results comprise three transfers **per direction per product**. Every
transfer passed exact byte-count and SHA-256 checks. All HTTPS bulk requests
returned HTTP 200. These are bounded payload rates, not sustained wire speed:
TLS/SSH setup, command execution and verification affect them. SSH used the
project's unmodified `bulk-check`, fresh connections, no compression or reuse,
and the same origin SSH server/key for both routes. Timed SSH download includes
remote random generation and local hashing; upload excludes local generation.
HTTPS upload read a pregenerated file; download served a pregenerated random
8 MiB file and was checked afterward. No Quick maximum-body-size claim is made.

## Measured resource use

Each row is a separate **60-second** window, sampled once per second. CPU is
time-weighted process CPU time / elapsed wall time; **100% = one logical core**,
so values over 100% are legitimate on these two-vCPU VMs. RSS is summed process
RSS, not total VM RAM, PSS, Go heap, or cgroup memory; shared pages may be counted
more than once. Peak RSS is the largest sampled simultaneous sum, including the
initial snapshot. Whole-VM CPU steal was zero in these sampled windows.

| Deployment / window | Load receiver Mbit/s | Visitor/relay CPU % | Origin CPU % | Visitor/relay peak RSS, MiB | Origin peak RSS, MiB |
| --- | --- | --- | --- | --- | --- |
| FRP, warmed idle | No benchmark load | 0.017 | 0.050 | 89.76 | 30.75 |
| Quick, warmed idle | No benchmark load | 0.000 | 0.317 | 110.70 | 167.25 |
| FRP, single-stream upload | 2,444.48 | 158.03 | 164.51 | 122.70 | 42.03 |
| FRP, single-stream download | 2,246.04 | 143.81 | 171.95 | 116.83 | 43.13 |
| Quick, single-stream upload | 271.72 | 5.60 | 68.45 | 108.96 | 171.74 |
| Quick, single-stream download | 619.25 | 56.79 | 97.74 | 113.62 | 174.43 |

Accounting boundaries matter:

- **FRP:** all running `frp-tunnel-*.service` processes, including Caddy, FRPS,
  FRPC, active socket proxies, SSH transport and managed SSH daemon, plus both
  privileged/unprivileged relay transport sshd processes outside those cgroups.
- **Quick:** **four separate origin connectors** (HTTP, iperf, echo, SSH) and
  **three visitor access clients** (iperf, echo, SSH), with one stream loaded at
  a time. The table sums this configured deployment, **not one cloudflared
  daemon**. Evidence retains per-unit CPU/RSS for alternate accounting.
- Quick origin connectors each had **one active QUIC edge connection**, observed
  in registration logs and metrics. A named tunnel can multiplex publications
  through one connector and normally uses four edge connections; do not apply
  this Quick process count or footprint to it [C2], [Q2].
- Fixtures, iperf, curl, administrative SSH and sampler processes are excluded.
  Kernel work outside counted processes and **Cloudflare's edge infrastructure**
  are also excluded. A zero sampled CPU value means no accounted CPU-tick delta,
  not zero operating-system work. Very short-lived processes can be missed.
- Idle followed application preflights; not every socket-activated FRP publication
  had been used yet. The loaded windows followed the transfer tests. Collectors
  and load were launched concurrently; small startup offsets are included.

The offered workload was one saturating stream, **not equal achieved throughput**.
The lower Quick CPU numbers therefore do not establish better CPU efficiency;
the public edge's CPU cost is unknown. Likewise, Cloudflare's published
4-CPU/4-GB deployment guidance [C1] is not a measured daemon footprint.

## Interruption and process-restart behavior

The matched fault was **SIGSTOP of the origin transport process for five
seconds, followed by SIGCONT**: OpenSSH transport for FRP, the echo connector for
Quick. The probe ran on the origin, using the public relay endpoint for FRP or
an origin-local `cloudflared access tcp` client for Quick. Both old-stream and
new-connection responses were observed blocked during every pause.

| Five trials per product | FRP tunnel | Cloudflare Quick Tunnel |
| --- | --- | --- |
| SIGCONT → first verified new response, median [range], seconds | **0.015 [0.012–0.050]** | **1.642 [0.133–23.774]** |
| Existing echo connections surviving the pause | **5/5** | **2/5** |

Timing starts immediately before the SIGCONT command, **excluding the injected
five-second pause**. New probes had a 0.2 s socket timeout, 0.1 s pause after
failure and a 30 s recovery deadline. Old-stream survival was checked after
new-request recovery, with a one-second timeout. The full five Quick recovery
times were **1.642, 23.774, 1.934, 0.138, 0.133 seconds**; the long result is not
an outlier discarded from the comparison. No client hostname configuration was
changed between these pause trials. These are process-suspension observations,
not a packet-loss experiment, relay-failure test, exact outage duration or SLA.

A **separate single Quick process restart** assigned a **different hostname**.
The old configured client failed to return the checked echo. Explicitly updating
the hostname and recreating the visitor access client restored a verified
22-byte echo. No automatic stable-hostname recovery time is claimed for this
case: controller SSH and configuration-refresh time were not part of the timed
fault comparison. This agrees with Quick Tunnel's documented ephemeral URLs [Q1].

Do not confuse pause survival with restart survival. The earlier FRP-only
`systemctl restart frp-tunnel-transport` experiment broke all five existing
connections; it was a different fault on a different VM pair, archived below.

## Environment, limits and reproduction

- Two newly provisioned **Hetzner CX23** VMs in **Nuremberg (`nbg1`)**, each with
  **2 shared vCPUs and 4 GB advertised RAM**. Both exposed **AMD EPYC-Rome** CPU
  models. They ran Ubuntu **24.04.4 LTS**, Linux **6.8.0-138-generic**,
  **FRP 0.71.0**, **OpenSSH 9.6p1 Ubuntu-3ubuntu13.19**, **iperf 3.16** and,
  on the relay, **Caddy 2.11.7**. Public-interface MTU was 1500; loopback 65536.
- **cloudflared 2026.9.3**, from the official versioned release [Q3], was verified
  against its published SHA-256 asset digest, retained in the evidence. Automatic
  updates were disabled. No Access login or account credentials were required;
  no DNS zone or named tunnel was created.
- Quick connectors registered through **Paris (`cdg11`/`cdg14`/`cdg13`)** before
  timing, using QUIC. Echo reconnected through other `cdg` instances during the
  fault experiments. The HTTP visitor edge was `CDG` throughout its 100 samples.
- **Stock socket settings were retained.** Kernel `rmem_max` and `wmem_max` were
  **212,992 bytes**; all four Quick connectors logged a UDP receive-buffer-size
  warning. No buffer tuning or HTTP/2 alternative-transport trial was performed.
  These results must not be advertised as optimized Cloudflare capacity.
- HTTPS used an explicitly trusted Caddy internal CA and loopback `--resolve`
  for FRP, versus ordinary public DNS and certificate validation for Quick.
  DNS, edge routing and certificate handshakes therefore differ. No insecure
  TLS switches were used. Only `public` HTTP access was measured, not Basic/OIDC,
  browser login, CDN cache hits, WAF effectiveness or public certificate issuance.
- p50 is the ordinary median; p95 is nearest-rank
  `sorted_samples[ceil(0.95*n)-1]`. Small three-trial transfer sets use median
  and range, not a meaningful p95 claim. All failed or partial probe records
  would be retained. No failures were retried away or replaced by another route.

Use isolated hosts and the normal [deployment procedure](deployment.md). Keep
all generated URLs, pins, keys, inventories and logs private. This run used
origin echo port 18081, HTTP port 18080, loopback iperf port 5201 and direct-control
iperf port 5202. HTTP returned `b'frp-benchmark-v1\n' * 64` for small probes,
streamed SHA-256/length responses for POSTs, and served a pregenerated random
8 MiB download, always with `Cache-Control: no-store`. Direct fixture ports were
firewalled to the peer and loopback. Backend fixtures held no real application data.

FRP publications used public ports 2233 (echo), 5201 (iperf), 2222 (managed SSH)
and 443 (Caddy), with corresponding relay-loopback ports 12223, 15201, 12222 and
12300. Start separate Quick origin processes for the same four backends:

```bash
cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:20241 --url http://127.0.0.1:18080
cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:20242 --url tcp://127.0.0.1:5201
cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:20243 --url tcp://127.0.0.1:18081
cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:20244 --url ssh://127.0.0.1:2222
```

Run them as separately supervised processes, retaining the assigned hostnames
privately. On the visitor, TCP access needs adapters; for example:

```bash
cloudflared --no-autoupdate access tcp --hostname "$QT_IPERF_HOST" --url 127.0.0.1:25201
```

Analogous echo/SSH adapters listened on 22334/22224. TLS WebSockets were verified
in this release's actual Quick setup; HTTP-centric Quick documentation alone
would not have proved these TCP/SSH paths. Ordinary OpenSSH then used private
profiles pointing at FRP port 2222 or Quick adapter port 22224, both pinned to
the same origin host key.

For each of three repetitions, interleave these on the relay, first without
`-R` (upload), then with `-R` (download):

```bash
iperf3 -c "$ORIGIN" -p 5202 -P 1 -t 30 -O 3 -C cubic --connect-timeout 10000 -J
iperf3 -c 127.0.0.1 -p 5201 -P 1 -t 30 -O 3 -C cubic --connect-timeout 10000 -J
iperf3 -c 127.0.0.1 -p 25201 -P 1 -t 30 -O 3 -C cubic --connect-timeout 10000 -J
```

The iperf subprocess deadline was 60 s; receiver rate is
`bytes * 8 / receiver_seconds / 1e6`. Recorded retransmissions belong to the
iperf TCP connection, not the outer SSH/QUIC network. Echo used `TCP_NODELAY` on
the visitor and checked every response. HTTPS used separate curl processes,
`--http1.1 --noproxy '*'`, a fresh nonce and no redirects. Application transfers
were sequential and bounded, not a 200-request concurrency or production load test.
The SSH check was:

```bash
python3 scripts/tunnel.py bulk-check \
  --ssh-config "$PRIVATE/client.conf" --host cf \
  --size-mib 64 --timeout 120 --json
```

Repeat for both private aliases three times. The 60-second resource windows
were separate from the 30-second throughput trials. Sample `/proc` CPU-time
deltas and RSS at one-second intervals using the accounting boundaries above;
retain actual intervals and per-unit readings. Do not substitute one `ps`
snapshot, an iperf CPU percentage or a TCP handshake for these measurements.

The throwaway probes are not a shipped benchmark harness. Sanitized records
are retained; temporary fixtures, credentials and private diagnostic logs are
not production inputs.
The temporary servers, their primary IPs and the temporary provider SSH key were
removed after capture; unrelated servers and existing account credentials were
left untouched.

## Tailscale: published context, not measured here

| Published metric | Reported observation | Scope and limitation |
| --- | --- | --- |
| Direct TCP dataplane goodput [T1] | **13.0 Gbit/s** bare metal; **7.29 Gbit/s** AWS | Vendor's optimized **wireguard-go** benchmark, 2023-04-13; not Funnel, DERP, or SSH file transfer. |
| Relayed TCP goodput [T2] | **2.20 Mbit/s DERP / 27.5 Mbit/s Peer Relay** | One Delhi–Minnesota field report, 2026-01-26; not universal limits or the newer GA implementation's benchmark. |
| Relayed diagnostic ping [T2] | **441–478 ms DERP**, then **306/298 ms Peer Relay** | Different routes, not HTTP/echo percentiles or isolated protocol overhead. |
| Comparable daemon RSS/CPU, recovery and Funnel throughput | **Not reported in reviewed sources** | Not zero or unlimited. No equivalent Tailscale workload was run here. |

[T1] used one iperf 3.9 TCP stream, Cubic, 10 seconds without omitted warm-up,
128 KiB blocks, Ubuntu 22.04, either two i5-12400/25-Gbit/s Mellanox systems on
DAC or two AWS c6i.8xlarge instances in `us-east-2b`. Optimizations were entering
Tailscale v1.40; this was not a released-v1.40 daemon benchmark. Its 0.035/0.126 ms
average pings are underlay baselines, not VPN overhead.

[T2] used 120-second iperf3 observations, Chicago DERP versus a residential-fiber
Peer Relay. Exact software/hardware, repeat count and reverse-direction results
were not supplied. It predates the 2026-02-18 Peer Relay GA announcement [T4].
These results cannot establish a speed ranking against the measured VPS pair.

## Product constraints remain different

| Property | FRP tunnel | Cloudflare | Tailscale |
| --- | --- | --- | --- |
| Public client | Native TCP clients; explicit IPv4 publications on available ports 1024–65535. Raw TCP does not itself add authentication/TLS. | TCP/SSH required client-side cloudflared in this test [C4]. Quick HTTPS needed no special client or account [Q1]. | Private connectivity needs Tailscale or an appropriate gateway. Public Funnel requires TLS, ports **443/8443/10000**, and tailnet `*.ts.net` names [T3]. |
| Limits and stability | Operator/backend policy; the matched 8 MiB POST passed. One relay, no automatic multi-relay HA. | Quick: **200 in-flight requests**, no SSE or uptime guarantee, changing hostname [Q1]. Named Tunnel's **4 connections to ≥2 data centers**, up to **25 replicas**, are **not this Quick setup** [C2]. | Funnel has non-configurable bandwidth limits without a numeric rate disclosed in [T3]. Direct mesh, DERP, Peer Relay and Funnel must not be conflated. |
| HTTP request-body limits | 8 MiB verified in this matched run; 128 MiB in the archived project-only run. Neither establishes a maximum. | General proxied-HTTP plans document **100 MB Free/Pro, 200 MB Business, Enterprise self-service up to 5 GB** [C5]. Do not relabel those as a verified Quick entitlement or an SSH transfer cap. | No comparable numeric Funnel body-size limit established by the reviewed sources. |
| Cost boundary | MIT code; operator pays for relay, egress and administration. | Quick needs no account; named Tunnel connectivity is available at no cost [C6], with optional services/hosting separate. Public-hostname large-file/video policy restrictions still matter [C7]. | Personal is free for **non-commercial** use; business plans are paid [T5]. Funnel is available on all plans [T3]; customer-hosted relay infrastructure still costs money. |

## Earlier project-only evidence

The [original FRP-only records](benchmark-results/2026-10-05-hetzner.json) and
[their original methodology](https://github.com/0xprincess/frp-tunnel/blob/2ca728a8c3a489368cba406fbc86a1e66a00e38d/docs/comparison.md)
remain available. That pair had an Intel relay/AMD origin and an earlier OpenSSH
package revision. Its Internet/SOCKS workstation transfers, 128 MiB POST and
explicit transport-restart measurements were **not rerun as matched Quick
experiments**. Do not pool those observations with this new pair or mistake the
new FRP numbers for a software performance improvement; project runtime code
was unchanged.

## Sources

Reviewed 2026-10-05; live documentation and pricing can change.

[Q1]: https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/
[Q2]: https://github.com/cloudflare/cloudflared/blob/2026.9.3/cmd/cloudflared/tunnel/quick_tunnel.go
[Q3]: https://github.com/cloudflare/cloudflared/releases/tag/2026.9.3
[C1]: https://developers.cloudflare.com/tunnel/platform/system-requirements/
[C2]: https://developers.cloudflare.com/tunnel/configuration/
[C4]: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/protocols/
[C5]: https://developers.cloudflare.com/cache/concepts/default-cache-behavior/#upload-limits
[C6]: https://blog.cloudflare.com/tunnel-for-everyone/
[C7]: https://developers.cloudflare.com/cloudflare-one/faq/cloudflare-tunnels-faq/#large-file-and-streaming-traffic-through-tunnel
[T1]: https://tailscale.com/blog/more-throughput
[T2]: https://tailscale.com/blog/peer-relays-international-networks
[T3]: https://tailscale.com/docs/features/tailscale-funnel
[T4]: https://tailscale.com/blog/peer-relays-ga
[T5]: https://tailscale.com/pricing

- [Q1 — Quick Tunnel setup and limits][Q1].
- [Q2 — Pinned Quick Tunnel implementation][Q2].
- [Q3 — cloudflared 2026.9.3 release][Q3].
- [C1 — Cloudflare system recommendations][C1].
- [C2 — Named Tunnel connection topology and replicas][C2].
- [C4 — Published application protocols][C4].
- [C5 — Proxied HTTP upload limits][C5].
- [C6 — Free Tunnel announcement][C6].
- [C7 — Large-file and streaming policy][C7].
- [T1 — Optimized wireguard-go throughput, 2023-04-13][T1].
- [T2 — DERP/Peer Relay field observations, 2026-01-26][T2].
- [T3 — Funnel requirements and limitations][T3].
- [T4 — Peer Relay general availability, 2026-02-18][T4].
- [T5 — Tailscale pricing and entitlements][T5].
