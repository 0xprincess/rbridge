# Cloudflare Tunnel and Tailscale comparison

Research and project measurements: **2026-10-05**. Project revision:
[`d216bdb`](https://github.com/0xprincess/frp-tunnel/commit/d216bdb705a7b4bf6eb653fce7504fca480bd6d7).

**This is not a controlled three-product benchmark.** Only FRP tunnel was
installed and measured here. The alternatives' figures below are published
observations or documented limits, explicitly labeled. Different CPUs, network
paths, protocols and dates prevent a numerical performance ranking.

Cloudflare public-hostname Tunnel, Tailscale private direct/relayed connectivity,
and Tailscale public Funnel are different access paths. In particular, a direct
WireGuard benchmark does not measure Funnel or DERP, and a CDN cache hit does
not measure a request through Cloudflare Tunnel to an origin.

## Critical metrics: observed versus reported

“Not reported” means **not reported in the reviewed sources**, not zero,
unlimited, or a claim that nobody has ever measured it.

| Metric | FRP tunnel: measured here | Cloudflare Tunnel: reported evidence | Tailscale: reported evidence |
| --- | --- | --- | --- |
| Sustained TCP receiver goodput | Median **1.460 Gbit/s upload / 1.807 Gbit/s download**, relay-local visitor to separate origin; three trials per direction. | No reproducible public-hostname TCP/SSH benchmark found in the reviewed primary sources. Historical community downloads are discussed below, not treated as a product rating. | **13.0 Gbit/s** bare-metal / **7.29 Gbit/s** AWS optimized **wireguard-go dataplane** benchmark [T1]. A separate field report found **2.20 Mbit/s DERP / 27.5 Mbit/s Peer Relay** [T2]. Neither measures Funnel; these are different workloads and routes. |
| Application latency | 1 KiB echo **1.947 ms p50 / 2.509 ms p95**; fresh HTTPS total **51.182 / 53.921 ms**. No Internet visitor leg. | No equivalent echo or uncached HTTPS latency distribution reported in reviewed primary sources. | Diagnostic pings **441–478 ms DERP**, then **306/298 ms Peer Relay** [T2]. Different routes, not application percentiles or protocol overhead. |
| Process RSS, sampled peak | Idle relay/origin **89.74 / 31.12 MiB**; peaks across the two load windows **111.39 / 42.78 MiB**. Full accounting below. | No comparable measured footprint reported. The **4 GB RAM / 4 CPU cores per host**, two-host recommendation [C1] is capacity guidance, **not observed cloudflared consumption**. | No comparable Linux daemon idle/load RSS benchmark reported in reviewed sources. |
| Process CPU | Idle relay/origin **0.067% / 0.017%**. Upload load **172.45% / 106.94%**; download load **171.59% / 118.22%**. **100% = one core**. | No comparable measured process-CPU result reported in reviewed primary sources. | No comparable daemon CPU result reported in reviewed sources; iperf's own CPU percentage is not daemon accounting. |
| Transport restart → verified new response | **2.219 s median**, **2.173–2.278 s** range, five explicit origin-transport restarts. Existing connection broke **5/5** times. | Not reported. Four connections and retry backoff are architecture/configuration, not a recovery measurement [C2][C3]. | Not reported for an equivalent controlled failure. Funnel performance and recovery distributions are also not reported [T3]. |

### What the published throughput numbers actually measured

- **[T1], 2023-04-13:** single-stream TCP, iperf 3.9, Cubic, 10 seconds,
  no omitted warm-up, 128 KiB blocks. Two Ubuntu 22.04 i5-12400 systems with
  25-Gbit/s Mellanox NICs connected by DAC, or two AWS c6i.8xlarge instances in
  `us-east-2b`. Linux kernels 5.15.0-69 and 5.19.0-1022-aws respectively.
  The article tests wireguard-go optimizations being incorporated into Tailscale
  v1.40, not a benchmark of a released v1.40 daemon. Its 0.035/0.126 ms average
  pings are **underlay baselines**, not Tailscale latency overhead.
- **[T2], 2026-01-26:** 120-second iperf3 TCP observations between Delhi and
  Robbinsdale, Minnesota. Chicago DERP versus a residential-fiber Peer Relay.
  Exact software versions, endpoint hardware, repeat count and reverse-direction
  results are not supplied. This predates the 2026-02-18 Peer Relay GA
  announcement [T4]; do not relabel it as a current-release benchmark.
- **[C8], 2023-09-09 community comment:** a 1 GB download reportedly reached
  **149/89 MB/s direct**, **152/94 MB/s through HTTP/2 Tunnel**, and **17/17 MB/s
  through QUIC Tunnel**, on two unspecified hosts. Units are quoted as reported.
  Hardware, route, cache status, repetition count and that commenter's version
  are unspecified. The issue opener's environment is not the commenter's
  environment. This is historical, uncontrolled evidence that transport choice
  can matter—not a vendor benchmark or an expected rate.

## Observed project measurements

The visitor for these same-site measurements ran **on the relay**. The published
path still traversed the real socket proxy or Caddy, FRPS, the encrypted
origin-to-relay SSH transport, FRPC, and the separate origin backend. It did
**not** include a remote visitor's Internet leg. The direct control connected
from relay to origin without FRP/SSH. These are deployment observations, not
maximum capacities or an Internet-access SLA.

[Sanitized measurements](benchmark-results/2026-10-05-hetzner.json) retain all
12 throughput trials, 1,000 timed echo samples, 100 HTTPS timings, 360 resource
samples, 12 verified SSH transfers, the HTTP upload result and five restarts.
Deployment addresses, account identifiers, keys, inventories and raw private
logs are intentionally excluded.

| Metric | FRP tunnel observation | Same-host-pair direct control |
| --- | --- | --- |
| Single-stream TCP upload, receiver median [range], 3 runs | **1,459.50 [1,448.07–1,510.66] Mbit/s** | **8,175.05 [7,500.42–8,687.57] Mbit/s** |
| Single-stream TCP download, receiver median [range], 3 runs | **1,807.08 [1,726.93–1,838.86] Mbit/s** | **7,491.19 [7,071.64–7,598.07] Mbit/s** |
| Persistent 1 KiB TCP echo, p50 / p95 | **1.947 / 2.509 ms**; 500 verified responses | **0.601 / 0.762 ms**; 500 verified responses |
| Fresh HTTPS 1,088-byte response, p50 / p95 total | **51.182 / 53.921 ms**; 100 correct HTTP 200 responses | Not measured with equivalent TLS |
| Fresh HTTPS time to first byte, p50 / p95 | **10.261 / 13.518 ms**; same 100 requests, measured from curl start | Not measured with equivalent TLS |
| Native SSH, 64 MiB per direction, median effective upload / download | **533.14 / 254.68 Mbit/s**, three verified transfers each direction | Not measured; includes SSH startup and verification work |
| Single HTTPS POST body | **128 MiB (134,217,728 bytes)**, HTTP 200, SHA-256 verified; **0.851 s**, one request | Not a maximum-size or sustained-throughput measurement |

### Resource use under a defined load

| Window | Relay CPU, % of one core | Origin CPU, % of one core | Relay peak RSS, MiB | Origin peak RSS, MiB | Load receiver goodput, Mbit/s |
| --- | --- | --- | --- | --- | --- |
| Warmed idle, 60 s | 0.067 | 0.017 | 89.74 | 31.12 | No offered benchmark traffic |
| Single-stream upload, 60 s | 172.45 | 106.94 | 111.39 | 35.79 | 1,421.72 |
| Single-stream download, 60 s | 171.59 | 118.22 | 105.89 | 42.78 | 1,858.86 |

These load windows are separate from the three 30-second throughput trials.
They show substantial CPU demand at the measured rates, not the capacity to
sustain those rates while also running a production application on the same VM.

### External workstation and recovery checks

- The actual WSL workstation used the documented
  [explicit SOCKS5 SSH profile](operations.md#native-ssh-access-on-filtered-networks).
  **Three 32 MiB uploads and three 32 MiB downloads** passed exact byte-count
  and SHA-256 checks. Median effective rates were **161.32 / 122.41 Mbit/s**;
  ranges **160.84–165.29 / 116.41–126.86 Mbit/s**. This includes the workstation,
  proxy route and Internet leg, so it must not be pooled with relay-local trials.
  The proxy's egress route was not characterized. No direct-WSL success is claimed.
- Five `systemctl restart frp-tunnel-transport` operations ran on the origin.
  Here the **probe also ran on the origin**, connecting to the relay's public
  echo publication and returning through the tunnel. Elapsed times from just
  before invoking systemctl to the first verified new response were
  **2.219, 2.276, 2.176, 2.278, 2.173 seconds**. The command itself returned in
  about 0.032 s; that was **not** recovery. Each pre-existing connection failed.
  New probes used a 0.2 s socket timeout and a 0.1 s pause after failure, after
  checking the old connection with a 1 s timeout. These are observed recovery
  bounds at that cadence, not exact outage durations. No blackholed link, relay
  reboot, host loss, uptime SLA or session-preserving failover was measured.

### Environment and measurement definitions

- Two newly provisioned **Hetzner CX23** VMs in **Nuremberg (`nbg1`)**, each
  with **2 shared vCPUs and 4 GB advertised RAM**. The relay exposed an Intel
  Xeon Skylake CPU model; the origin exposed AMD EPYC-Rome. Shared placement and
  CPU model differences limit reproducibility even with the same instance type.
- Both ran **Ubuntu 24.04.4 LTS**, Linux **6.8.0-138-generic**, **FRP 0.71.0**,
  **OpenSSH 9.6p1 Ubuntu-3ubuntu13.18**, and **iperf 3.16**. Relay web ingress
  used **Caddy 2.11.7**. Stock project roles and transport settings; no special
  performance tuning. Native public SSH, TCP echo, iperf and public-policy HTTPS
  were configured. No OIDC or Basic authenticator was included in the footprint.
- Echo: a single persistent socket, 1,024 bytes sent and checked per response;
  20 warm-up exchanges excluded, followed by 500 samples with a 10 ms pause
  between exchanges. Client `TCP_NODELAY` enabled. Timing excludes connection
  establishment. p50 is the ordinary median; p95 is nearest-rank
  `sorted_samples[ceil(0.95*n)-1]`. No timed samples were discarded.
- HTTPS: 100 separate curl processes and fresh HTTP/1.1/TLS connections;
  hostname resolved explicitly to relay loopback. Caddy's internal CA was
  explicitly trusted—no insecure TLS switch. Each origin response was 1,088
  checked bytes with `Cache-Control: no-store`; no CDN, DNS lookup, browser,
  public-ACME issuance, authentication flow or Internet visitor leg was timed.
- Throughput: one iperf3 TCP stream, Cubic, **30 timed seconds after 3 omitted
  warm-up seconds**, three trials per direction/path. Each repetition alternated
  direct/published upload, then direct/published download; no simultaneous load.
  Upload means visitor on relay → origin; download means origin → visitor.
  Receiver goodput is `received_bytes * 8 / receiver_seconds / 1e6` Mbit/s.
  This is not SSH file-copy speed, and iperf CPU utilization is not full tunnel
  CPU accounting. Recorded retransmissions describe the iperf TCP connection,
  not all packets or retransmissions of the outer SSH transport.
  iperf reported an initial TCP MSS of 1,448 bytes for direct trials and 32,768
  bytes for the published loopback entry; socket termination and MSS therefore
  also differ. This is a full deployment comparison, not isolated FRP overhead.
- Resources: 60-second windows, sampled once per second. Sum RSS and CPU ticks
  of processes in project `frp-tunnel-*.service` cgroups, **plus the relay's
  restricted transport sshd processes**. This includes Caddy, FRP, active socket
  proxies, origin SSH transport and managed SSH daemon. RSS can double-count
  shared pages; it is not PSS, Go heap size, cgroup memory or total VM RAM.
  CPU is process CPU-time delta / wall time; 100% means one logical core, not
  the entire two-core VM. Backend fixtures, iperf, curl, administrative SSH and
  kernel work outside those processes are excluded. Idle was sampled after
  echo/HTTPS requests; unused socket-activated services need not yet be running.
  CPU means are time-weighted over each window. Upload/download load used one
  60-second iperf stream, with collectors and load launched concurrently;
  small remote-start timing differences are included in the windows.
- SSH: the repository's unmodified `bulk-check` used fresh pinned OpenSSH
  connections, compression and reuse disabled, with random payloads and
  byte-count/SHA-256 verification. Timed upload excludes local payload generation;
  timed download includes remote random generation, transfer and local hashing.
  Both include SSH startup/authentication and remote command execution.
  These effective payload rates are not isolated wire speed or disk-copy rates.
- The 128 MiB HTTP request was generated before timing and uploaded from a
  temporary file. The origin streamed its length and SHA-256 into a small
  response; curl verified TLS and HTTP status, and the controller checked both
  returned values. Success proves this body size on this configuration, not an
  unlimited request size.

### Repeating the workloads

Use isolated hosts and the normal [deployment procedure](deployment.md).
Keep all addresses and credentials in private inputs. In this run the origin
hosted an echo fixture on 18081, a no-store 1,088-byte HTTP fixture on 18080, iperf
bound to loopback 5201 for publication, and a separate direct-control iperf
listener on 5202. Direct fixture ports were firewalled to the relay; published
benchmark TCP ports were limited to loopback and the origin. SSH administration
remained separately pinned.

On the relay, run each direction/path three times in the interleaved order
described above; `$ORIGIN` is the private direct-control address:

```bash
iperf3 -c "$ORIGIN" -p 5202 -P 1 -t 30 -O 3 -C cubic -J
iperf3 -c 127.0.0.1 -p 5201 -P 1 -t 30 -O 3 -C cubic -J
iperf3 -c "$ORIGIN" -p 5202 -P 1 -t 30 -O 3 -C cubic -J -R
iperf3 -c 127.0.0.1 -p 5201 -P 1 -t 30 -O 3 -C cubic -J -R
```

The published iperf service used origin backend `127.0.0.1:5201`, relay-loopback
FRP port 15201 and public port 5201. Echo used backend 18081, relay-loopback
12223 and public 2233. HTTPS used relay-loopback 12300 and public 443, with an
explicitly trusted internal CA and `public` access policy. Native SSH used the
default managed SSH publication. A matching verified-transfer command is:

```bash
python3 scripts/tunnel.py bulk-check \
  --ssh-config "$PRIVATE/client.conf" --host benchmark \
  --size-mib 64 --timeout 60 --json
```

Run that three times from the relay; for the separate workstation experiment,
use the explicit SOCKS profile, `--size-mib 32 --timeout 120`. For resource
sampling, read `/proc/PID/stat` CPU ticks and RSS for the accounting boundaries
above at one-second intervals; sum RSS, convert ticks with `SC_CLK_TCK`, and
divide CPU time by actual elapsed wall time. Keep per-sample records rather than
using a single `ps` snapshot. Follow the echo, HTTPS and recovery definitions
above; do not replace their checked responses with TCP handshakes.

The throwaway probes are not a supported benchmark harness. Only the sanitized
measurement record is retained in the repository; temporary fixtures and
credentials are not production deployment inputs.
Both temporary VPSs and their two primary IPs were deleted after capture;
pre-existing cloud servers were left untouched.

## Operational limits, not performance measurements

| Property | FRP tunnel | Cloudflare Tunnel | Tailscale |
| --- | --- | --- | --- |
| Public native TCP/SSH client | Ordinary native clients; explicit IPv4 TCP publications on available ports 1024–65535. Raw TCP does not itself add TLS or authentication. | Public-hostname TCP/SSH uses WebSockets and client-side `cloudflared access tcp/ssh`; vendor recommends Client-to-Tunnel for long-lived sessions [C4]. Paid Spectrum is a separate product, not this baseline. | Private tailnet access requires Tailscale or an appropriate network gateway. Public **Funnel** requires TLS, permits only **443/8443/10000**, and only tailnet `*.ts.net` names [T3]. Its TCP forwarder does not make unaided plaintext SSH equivalent. |
| HTTP request-body limit | **128 MiB single POST verified here**. Operator/backend configuration governs; this is not a maximum or unlimited-capacity claim. | **100 MB Free/Pro; 200 MB Business; Enterprise self-service up to 5 GB**, subject to zone settings and timeouts [C5]. These are HTTP request limits, not TCP/SSH throughput limits. | Funnel documents non-configurable bandwidth limits but **does not disclose a numeric rate** in [T3]. Do not substitute a guessed Mbit/s or body-size limit. |
| Redundancy | The measured deployment had one relay and one outbound SSH transport; no automatic multi-relay HA. | **4 connections to at least 2 Cloudflare data centers** per connector; up to **25 replicas / 100 connections per tunnel** [C2]. This is not an uptime percentage. | Direct, DERP and customer-hosted Peer Relay paths must be distinguished. Funnel is a separate public relay path [T2][T3][T4]. |
| Cost boundary | MIT project code; operator pays for and maintains relay infrastructure and egress. The second VPS here served as a disposable test origin. | Tunnel connectivity is available at no cost; optional services and origin hosting are separate [C6]. Free Tunnel does **not** imply unrestricted public large-file/video delivery; public-hostname policy restrictions apply [C7]. | Personal is free for **non-commercial** use; business plans are paid [T5]. Funnel is available on all plans [T3]; self-hosted Peer Relay infrastructure still has a cost. |

Cloudflare's baseline recommendation [C1] is not evidence that this project's
measured RSS makes it proportionally more efficient. Likewise, Tailscale's
high-throughput direct dataplane result does not predict Funnel performance.
Choose first by required access path, trust model and operational ownership;
benchmark the intended origin and visitor networks before choosing by speed.

## Sources

Sources reviewed on 2026-10-05. Publication dates are stated above for
benchmarks; live product documentation and pricing can change.

[C1]: https://developers.cloudflare.com/tunnel/platform/system-requirements/
[C2]: https://developers.cloudflare.com/tunnel/configuration/
[C3]: https://developers.cloudflare.com/tunnel/reference/run-parameters/
[C4]: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/protocols/
[C5]: https://developers.cloudflare.com/cache/concepts/default-cache-behavior/#upload-limits
[C6]: https://blog.cloudflare.com/tunnel-for-everyone/
[C7]: https://developers.cloudflare.com/cloudflare-one/faq/cloudflare-tunnels-faq/#large-file-and-streaming-traffic-through-tunnel
[C8]: https://github.com/cloudflare/cloudflared/issues/895
[T1]: https://tailscale.com/blog/more-throughput
[T2]: https://tailscale.com/blog/peer-relays-international-networks
[T3]: https://tailscale.com/docs/features/tailscale-funnel
[T4]: https://tailscale.com/blog/peer-relays-ga
[T5]: https://tailscale.com/pricing

- [C1 — Cloudflare system requirements][C1].
- [C2 — Cloudflare connection topology and replicas][C2].
- [C3 — cloudflared run parameters][C3].
- [C4 — Published application protocols][C4].
- [C5 — Cloudflare HTTP upload limits][C5].
- [C6 — Free Tunnel announcement, 2021-04-15][C6].
- [C7 — Tunnel large-file and streaming policy][C7].
- [C8 — Historical community throughput observations][C8].
- [T1 — Optimized wireguard-go throughput, 2023-04-13][T1].
- [T2 — DERP/Peer Relay field observations, 2026-01-26][T2].
- [T3 — Funnel requirements and limitations][T3].
- [T4 — Peer Relay general availability, 2026-02-18][T4].
- [T5 — Tailscale pricing and usage entitlements][T5].
