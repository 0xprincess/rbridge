# Cloudflare Tunnel and Tailscale comparison

Measured **2026-10-05**, in **two separate matched batches**:

- **Tailscale 1.102.4**, both private mesh and public Funnel, versus this project
  at revision [`496eac6`](https://github.com/0xprincess/frp-tunnel/commit/496eac672d65763546e517cca7e1319202cd249f).
- Earlier **cloudflared 2026.9.3 Quick Tunnel** versus project revision
  [`2ca728a`](https://github.com/0xprincess/frp-tunnel/commit/2ca728a8c3a489368cba406fbc86a1e66a00e38d).

Each batch used a fresh **Hetzner CX23 pair in Nuremberg (`nbg1`)**, with
**2 shared vCPUs and 4 GB advertised RAM per VM**. Within a batch, products used
one visitor and the same origin backends. The Tailscale pair had an **AMD relay
and Intel origin**; the earlier Quick pair was AMD/AMD. Software package revisions
also differed. **Compare against the FRP baseline in the same batch; do not pool
the batches into a three-product speed ranking.** Project runtime code was unchanged.

The visitor ran on the FRP relay: FRP ingress was local, private mesh reached the
origin over Tailscale, and public services traversed their relay/edge networks.
These are measured end-to-end paths, not equal-distance transports, isolated
cryptographic overhead, typical Internet-user latency, or universal speed ratings.

## Tailscale: matched transport measurements

[Sanitized Tailscale evidence](benchmark-results/2026-10-05-tailscale.json) retains
**24 TCP trials, 2,000 timed echo samples, 300 HTTPS requests, 18 verified HTTPS
bulk transfers, 18 verified SSH transfers, 840 resource samples, 15 pause trials
and two process restarts**. All normal workload records passed. Four resource
samples have explicit scan warnings, described below; they were not discarded.
Direct-control HTTPS/SSH were not measured, and their unavailable slots remain
in the evidence rather than being counted as failures or successful transfers.
Addresses, assigned hostnames, identities, keys and private diagnostics are excluded.

The four paths were:

- **Direct control:** visitor on relay → origin's peer-firewalled public fixture.
- **FRP:** visitor → relay-local public socket → FRPS → persistent outbound
  OpenSSH transport → FRPC → origin backend. HTTPS additionally uses relay Caddy.
- **Private mesh:** visitor `tailscaled` → origin `tailscaled`, using the kernel
  TUN interface. TCP/echo reach the same fixture listeners; HTTPS terminates at
  origin Tailscale Serve. Ordinary OpenSSH uses a private Serve TCP proxy to the
  same managed SSH backend. **Tailscale SSH was disabled.**
- **Public Funnel:** ordinary public DNS → public Funnel relays → origin
  `tailscaled` → the same backends. TCP/SSH additionally use visitor **socat TLS
  adapters with certificate, hostname and SNI verification**. This is not a native
  plaintext public TCP socket, and is not the private mesh route.

TCP cells are decimal **Mbit/s**, **median [minimum–maximum]**, three trials per
direction/path: one Cubic stream, 30 timed seconds after three omitted warm-up
seconds. Each repetition interleaved direct/FRP/mesh/Funnel uploads, then their
downloads. Upload means visitor → origin; download is the reverse.

| Metric | Direct control | FRP tunnel | Tailscale private mesh | Public Funnel |
| --- | --- | --- | --- | --- |
| TCP upload, Mbit/s | **6,680.51 [6,080.85–7,092.25]** | **1,622.35 [1,472.79–1,675.68]** | **1,251.98 [1,227.57–1,261.29]** | **14.67 [14.01–19.39]** |
| TCP download, Mbit/s | **8,165.69 [8,117.27–8,431.56]** | **1,329.11 [1,323.48–1,362.80]** | **1,391.77 [1,329.04–1,486.64]** | **21.74 [20.87–21.78]** |
| Persistent 1 KiB echo, p50 / p95, ms | **0.789 / 0.955** | **2.115 / 2.600** | **1.745 / 2.076** | **11.771 / 59.308** |

Echo used one persistent connection per path, 20 excluded warm-ups, then 500
integrity-checked 1,024-byte replies with a 10 ms pause between exchanges.
Connection setup is excluded; no timed samples were discarded. p50 is the median;
p95 is nearest-rank `sorted_samples[ceil(0.95*n)-1]`. Three-trial transfer sets
use median/range, not a meaningful p95 claim.

### Observed routing, not assumed routing

During the transfer schedule, five-second status sampling captured **151 active
peer observations on the visitor and 161 on the origin; all had a direct endpoint**.
Every sampled interval carrying more than 1 MiB of peer traffic also had a direct
endpoint: **55/55 visitor, 56/56 origin**. No Peer Relay was observed. The preferred
DERP region was **Frankfurt (`fra`)**; that field alone does **not** mean data used
DERP. Idle peers sometimes had no current endpoint. These are sampled status
observations, not proof of every packet's route [T10].

All **100 Funnel HTTPS** peers were public addresses; all **122 sampled outbound
Funnel-adapter sockets** were public too. All private HTTPS peers were tailnet
addresses, forced by `--resolve` while retaining the same hostname and certificate
validation. No forced-DERP, Peer Relay, IPv6, cross-region or blocked-UDP workload
was measured. Do not transfer the direct-mesh numbers to those cases.

### HTTPS and ordinary OpenSSH

All three products returned the same **1,088-byte** body in 100 requests each,
using fresh HTTP/1.1/TLS connections, unique query nonces, verified certificates
and `Cache-Control: no-store`. FRP trusted an explicit Caddy internal CA and used
loopback `--resolve`; private Tailscale resolved the Funnel hostname to its tailnet
IP; Funnel used ordinary public DNS. DNS, TLS termination location and certificate
chains therefore differ. No insecure TLS switches, redirects, compression,
connection reuse, CDN cache hits or browser authentication were measured.

| Metric | FRP tunnel | Tailscale private mesh | Public Funnel |
| --- | --- | --- | --- |
| Fresh HTTPS time to first byte, p50 / p95, ms | **7.500 / 9.098** | **32.135 / 42.204** | **67.829 / 1,089.455** |
| Fresh HTTPS total time, p50 / p95, ms | **7.662 / 9.353** | **32.287 / 42.348** | **67.995 / 1,089.600** |
| Verified 8 MiB HTTPS upload / download, median effective Mbit/s | **559.37 / 978.88** | **351.63 / 617.59** | **12.64 / 21.80** |
| Verified 64 MiB SSH upload / download, median effective Mbit/s | **273.77 / 383.75** | **371.02 / 368.48** | **12.32 / 21.40** |

Funnel's fresh-HTTPS tail is material: **6/100 requests exceeded one second**;
the maximum total time was **2,758.618 ms**. Those successful but slow requests
remain in the percentiles. The observed rates do not establish a Funnel hard cap.

Bulk results are three transfers **per direction per product**, all passing exact
byte-count and SHA-256 checks. All HTTPS bulk requests returned HTTP 200. These
are bounded payload rates, not sustained wire speed: connection setup, command
execution and verification affect them. HTTPS uploaded/served pregenerated random
8 MiB files. Its rate uses curl `time_total`; downloaded content is checked afterward.
SSH used the project's unmodified `bulk-check`, fresh connections, no compression
or reuse, and the same origin server/key for all routes. SSH download timing
includes remote random generation and local hashing; upload excludes local generation.
Neither the 8 MiB fixture bound nor the 64 MiB SSH payload establishes a product limit.

### CPU and RSS

Each resource window lasted **60 seconds**, sampled once per second on both hosts:
one warmed-idle window and six separately loaded windows. CPU is total accounted
process CPU time / actual elapsed time; **100% = one logical core**. RSS is the
largest sampled simultaneous sum, including the initial snapshot, **not** PSS,
Go heap, cgroup memory or total VM RAM; shared pages may be counted more than once.
All **840 sampled VM steal deltas were zero**.

| Deployment / window | Load receiver Mbit/s | Visitor/relay CPU % | Origin CPU % | Visitor/relay peak RSS, MiB | Origin peak RSS, MiB |
| --- | --- | --- | --- | --- | --- |
| FRP, warmed idle | No benchmark load | 0.017 | 0.067 | 108.90 | 37.96 |
| Private mesh, warmed idle | No benchmark load | 0.067 | 0.183 | 59.75 | 158.72 |
| Funnel, warmed idle | No benchmark load | 0.000 | 0.183 | 11.25 | 158.72 |
| FRP, single-stream upload † | 1,535.13 | 110.25 | 160.37 | 115.27 | 40.34 |
| FRP, single-stream download † | 1,211.78 | 113.23 | 171.52 | 106.74 | 45.83 |
| Private mesh, single-stream upload † | 1,219.09 | 54.03 | 144.13 | 72.74 | 273.49 |
| Private mesh, single-stream download | 1,424.71 | 92.20 | 135.05 | 198.86 | 273.49 |
| Funnel, single-stream upload | 13.89 | 0.55 | 53.90 | 24.83 | 275.11 |
| Funnel, single-stream download † | 18.87 | 4.25 | 54.17 | 24.95 | 269.29 |

**† Four origin windows each contain one `process_disappeared_during_scan` warning
in one sample.** The process vanished before the scanner could establish whether
it belonged to an included scope. These rows can undercount CPU/RSS; no sample
or window was dropped or rerun. Relay windows had no scan warnings.

Accounting boundaries:

- **FRP:** all `frp-tunnel-*.service` processes, including Caddy, FRPS/FRPC,
  active socket proxies, SSH transport and managed SSH daemon, plus both restricted
  relay transport sshd privilege roles outside those service cgroups.
- **Private mesh:** `tailscaled` on each host.
- **Funnel:** origin `tailscaled` plus all **three visitor TLS adapter services
  and their children**. Visitor `tailscaled` is excluded from this public-path
  total; it is not needed by the public TLS client. Per-unit readings allow other
  accounting choices.
- **The same origin daemon serves mesh and Funnel.** Idle follows all application
  transfers; mesh load precedes Funnel load. Funnel RSS retains earlier mesh
  allocations and is **not a standalone cold-start footprint**. The two origin
  idle entries intentionally describe the same daemon, not additive deployments.
- Fixtures, iperf, curl, administrative SSH, sampler processes and SSH shell/hash/
  generator workloads are excluded. Kernel work outside counted processes and
  public relay infrastructure are unobserved. Status-watcher processes are excluded,
  but their daemon RPC work remains included. Zero sampled CPU means no accounted
  tick delta, not zero OS work; short-lived processes can be missed.

Load and collectors started concurrently, with small startup offsets included.
These separate 60-second loads had no omitted warm-up. The workload was one
saturating stream, **not equal achieved throughput**; CPU percentages do not rank
CPU efficiency, especially when Funnel's public relay cost is unknown.

### Five-second interruption and process restart

The matched fault was **SIGSTOP of the origin transport process for five seconds,
then SIGCONT**: OpenSSH transport for FRP, `tailscaled` for mesh/Funnel. The visitor
was the **relay for every route**. Old-stream and new-connection responses were
observed blocked during all 15 pauses.

| Five trials per route | FRP tunnel | Tailscale private mesh | Public Funnel |
| --- | --- | --- | --- |
| SIGCONT command start → first verified new response, median [range], seconds | **0.037 [0.033–0.074]** | **0.042 [0.033–0.042]** | **0.127 [0.117–0.137]** |
| Existing echo streams passing the post-recovery check | **5/5** | **5/5** | **4/5** |

Timing starts immediately before issuing SIGCONT over an **already-authenticated,
multiplexed administrative SSH connection on the ordinary public network**. It
excludes the five-second pause but **includes 27–40 ms of management-command time**.
New probes used a 0.2 s socket timeout, 0.1 s delay after failure and a 30 s deadline.
The old stream had to return the checked payload within one second after new-request
recovery. A failed old-stream check is not proof of its precise TCP close time.
These are process-suspension observations, not packet-loss or relay-failure tests,
exact outage durations, or SLAs. The earlier Quick batch used a different probe
location/control clock; its small FRP recovery times are not directly comparable.

Separate **single** `systemctl restart` observations:

| Restarted origin process | Checked route | Command start → first verified new echo, seconds | Existing echo passed afterward | Client configuration refresh |
| --- | --- | --- | --- | --- |
| FRP OpenSSH transport | FRP | **2.389** | No | Not needed |
| tailscaled | Private mesh | **0.956** | Yes | Not needed |
| Same tailscaled restart | Public Funnel | **4.382** | No | Not needed |

The Tailscale node ID, tailnet IPs and hostname remained unchanged with persistent
on-disk daemon state. Both measured echo routes recovered without a hostname refresh.
The surviving private stream was the **kernel/TUN echo path**, not Serve-proxied
SSH; do not generalize its survival to all applications. These are **n=1** restart
observations, not percentile estimates, guarantees or an HA design.

### Tailscale environment and reproduction

- Ubuntu **24.04.4 LTS**, Linux **6.8.0-138-generic**, **FRP 0.71.0**,
  **OpenSSH 9.6p1 Ubuntu-3ubuntu13.18**, **iperf 3.16**, relay **Caddy 2.11.7**.
  Relay CPU: **AMD EPYC-Rome**; origin: **Intel Xeon (Skylake, IBRS, no TSX)**.
  Software metadata was unchanged after the trials.
- **Tailscale 1.102.4**, official static amd64 archive, SHA-256 verified on both
  hosts against the published digest [T6]. The digest and exact version metadata
  are retained in the evidence, including socat version components.
- Kernel TUN, `--accept-dns=false --accept-routes=false --ssh=false`, persistent
  local state. Public-interface MTU **1500**, loopback **65536**, Tailscale **1280**.
  Stock socket buffers (`rmem_max`/`wmem_max` **212,992 bytes**); no manual tuning.
- A temporary, short-lived ephemeral enrollment key was revoked after the two
  nodes joined. Existing supplied credentials were not changed. HTTPS and MagicDNS
  were already enabled. Funnel permission was granted only to the temporary origin,
  with policy validation and ETag protection, after approval of public exposure.
  Public certificate issuance leaves a Certificate Transparency record [T9].
- One initial relay allocation failed at the provider before SSH or measurement;
  its same-SKU/location replacement is the relay described above. Funnel DNS was
  allowed to become publicly resolvable before timing; no DNS-propagation duration
  is claimed.

Use isolated hosts and the normal [deployment procedure](deployment.md). Keep
inventories, generated keys, host pins, assigned URLs and raw logs private. The
origin fixture provides echo on **18081**, HTTP on loopback **18080**, iperf on
**5201**, and a separate direct-control iperf listener on **5202**. Only the peer
and loopback may reach raw fixture/public benchmark sockets. Management SSH is
separate. No real application data is exposed.

The HTTP probe is `b'frp-benchmark-v1\n' * 64`; POST replies contain SHA-256/length,
and GET downloads serve a pregenerated random 8 MiB file, all with `no-store`.
FRP publishes echo **2233**, iperf **5201**, managed SSH **2222**, Caddy **443**.
Tailscale's matching origin publications were [T7], [T8]:

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:18080
tailscale funnel --bg --tls-terminated-tcp=8443 tcp://127.0.0.1:5201
tailscale funnel --bg --tls-terminated-tcp=10000 tcp://127.0.0.1:18081
tailscale serve --bg --tcp=2222 tcp://127.0.0.1:2222
```

For SSH trials only, switch public port **10000** from echo to the same loopback
SSH backend, then restore echo. HTTPS stays on 443 and iperf on 8443. The visitor's
three supervised adapters listen only on loopback **25201 / 22334 / 22224**; for example:

```bash
socat TCP4-LISTEN:25201,bind=127.0.0.1,reuseaddr,fork,nodelay \
  "OPENSSL:$FUNNEL_HOST:8443,verify=1,cafile=/etc/ssl/certs/ca-certificates.crt,commonname=$FUNNEL_HOST,snihost=$FUNNEL_HOST,nodelay"
```

For each of three repetitions, run these from the relay, first as upload, then
with `-R` for download. Each subprocess has a 60 s outer deadline:

```bash
iperf3 -c "$ORIGIN_PUBLIC" -p 5202 -P 1 -t 30 -O 3 -C cubic --connect-timeout 10000 -J
iperf3 -c 127.0.0.1 -p 5201 -P 1 -t 30 -O 3 -C cubic --connect-timeout 10000 -J
iperf3 -c "$ORIGIN_TS4" -p 5201 -P 1 -t 30 -O 3 -C cubic --connect-timeout 10000 -J
iperf3 -c 127.0.0.1 -p 25201 -P 1 -t 30 -O 3 -C cubic --connect-timeout 10000 -J
```

Receiver goodput is `bytes * 8 / receiver_seconds / 1e6`; reported retransmissions
belong to iperf's TCP connection, not its outer transport. Echo enables
`TCP_NODELAY` and verifies each reply. HTTPS uses a separate curl process with
`--http1.1 --noproxy '*'`, a fresh nonce, 60 s curl timeout and 65 s outer deadline.
Private HTTPS resolves the certificate hostname to `$ORIGIN_TS4`; public Funnel
must not use that override. These are sequential bounded workloads, not concurrency
or production-capacity tests.

Pin the same managed SSH host key in three private aliases (`frp`, `tailscale`,
`funnel`), targeting their respective sockets. Run each alias three times:

```bash
python3 scripts/tunnel.py bulk-check \
  --ssh-config "$PRIVATE/client.conf" --host tailscale \
  --size-mib 64 --timeout 120 --json
```

Resource capture uses `/proc` CPU-time deltas/RSS at one-second intervals, not a
single `ps` snapshot or iperf's CPU percentage. The evidence retains per-unit
counters, actual intervals, scan errors and routing observations. Throwaway probe
scripts are not a shipped benchmark harness.

After capture, removal of the temporary servers, primary IPs, provider SSH key and
Tailscale nodes, and revocation of the temporary enrollment key, were verified. The original tailnet
policy was restored exactly; existing servers, devices, credentials and HTTPS
settings were preserved. Temporary private deployment inputs and diagnostics are
not repository content.

## Earlier Cloudflare Quick Tunnel batch

[Quick Tunnel evidence](benchmark-results/2026-10-05-quick-tunnel.json) and
[its complete original methodology](https://github.com/0xprincess/frp-tunnel/blob/496eac672d65763546e517cca7e1319202cd249f/docs/comparison.md)
remain unchanged. That **AMD/AMD CX23 nbg1 pair** ran **cloudflared 2026.9.3**,
OpenSSH **9.6p1 Ubuntu-3ubuntu13.19**, and the same listed FRP/Caddy/iperf/kernel
versions. Quick is **not a named production tunnel**. TCP/SSH used actual
client-side cloudflared TLS WebSocket adapters, not a native public TCP socket.

There were **18 TCP trials, 1,500 timed echoes, 200 HTTPS requests, 12 verified
HTTPS bulk transfers, 12 verified SSH transfers, 600 resource samples, ten pauses
and one Quick restart**. Timed transfers had no failed records. The same
30 s/3 s omitted, n=3 throughput and n=500 echo methodology produced:

| Metric | Direct control | FRP tunnel | Cloudflare Quick Tunnel |
| --- | --- | --- | --- |
| TCP upload, Mbit/s | **5,585.39 [5,577.40–6,288.68]** | **2,479.09 [2,428.59–2,486.00]** | **292.08 [281.56–303.41]** |
| TCP download, Mbit/s | **8,862.02 [8,749.45–8,924.36]** | **2,306.14 [2,246.54–2,322.56]** | **725.65 [704.85–762.18]** |
| Persistent 1 KiB echo, p50 / p95, ms | **0.511 / 0.742** | **1.362 / 1.918** | **29.614 / 30.332** |

Fresh HTTPS used 100 verified 1,088-byte responses per product, with nonces and
`no-store`; all Quick responses reported **`CF-Cache-Status: DYNAMIC`** and visitor
edge **`CDG`**, not cache hits. Bulk transfers were n=3 per direction/product,
with exact byte counts/SHA-256 and the same bounded-payload methodology above.

| Metric | FRP tunnel | Cloudflare Quick Tunnel |
| --- | --- | --- |
| Fresh HTTPS time to first byte, p50 / p95, ms | **6.262 / 9.601** | **124.583 / 158.739** |
| Fresh HTTPS total time, p50 / p95, ms | **6.386 / 9.785** | **124.821 / 158.906** |
| Verified 8 MiB HTTPS upload / download, median effective Mbit/s | **689.38 / 1,194.60** | **160.20 / 300.08** |
| Verified 64 MiB SSH upload / download, median effective Mbit/s | **651.54 / 521.74** | **211.37 / 300.60** |

### Earlier resource and recovery observations

Separate 60 s, 1 Hz windows; CPU 100% = one core, summed peak RSS, zero sampled
VM steal. **Quick totals include four origin connectors (HTTP, iperf, echo, SSH)
and three visitor access clients**, one stream loaded at a time—not one daemon.
FRP used the same service-plus-restricted-sshd accounting boundary as above.

| Deployment / window | Load receiver Mbit/s | Visitor/relay CPU % | Origin CPU % | Visitor/relay peak RSS, MiB | Origin peak RSS, MiB |
| --- | --- | --- | --- | --- | --- |
| FRP, warmed idle | No benchmark load | 0.017 | 0.050 | 89.76 | 30.75 |
| Quick, warmed idle | No benchmark load | 0.000 | 0.317 | 110.70 | 167.25 |
| FRP, single-stream upload | 2,444.48 | 158.03 | 164.51 | 122.70 | 42.03 |
| FRP, single-stream download | 2,246.04 | 143.81 | 171.95 | 116.83 | 43.13 |
| Quick, single-stream upload | 271.72 | 5.60 | 68.45 | 108.96 | 171.74 |
| Quick, single-stream download | 619.25 | 56.79 | 97.74 | 113.62 | 174.43 |

Idle followed application preflights, before every socket-activated FRP publication
had been used; loaded windows followed transfers. Fixtures/admin/samplers, kernel
work outside counted processes and Cloudflare's edge are excluded. Unequal achieved
throughput prevents a CPU-efficiency ranking. Published 4-CPU/4-GB guidance [C1]
is not a measured daemon footprint.

Each Quick origin connector had **one active QUIC edge connection**, registered
through Paris (`cdg11`/`cdg14`/`cdg13`), with `cdg` reconnections during faults.
All four logged UDP receive-buffer warnings with stock **212,992-byte** limits.
No tuning or HTTP/2 alternative was tried. Named Tunnel's connection/process
model differs [C2], [Q2]; these are not optimized or production-Tunnel capacity claims.

The five-second SIGSTOP/SIGCONT trials paused FRP's SSH transport or Quick's echo
connector. Unlike the Tailscale batch, probes ran **on the origin**, via the public
FRP endpoint or an origin-local cloudflared adapter; SIGCONT was issued locally.

| Five trials per product | FRP tunnel | Cloudflare Quick Tunnel |
| --- | --- | --- |
| SIGCONT → first verified new response, median [range], seconds | **0.015 [0.012–0.050]** | **1.642 [0.133–23.774]** |
| Existing echo connections surviving the pause | **5/5** | **2/5** |

All pauses blocked old and new responses. Timing excludes the pause; new probes
used 0.2 s timeouts, 0.1 s retry delays, a 30 s deadline and a one-second old-stream
check after recovery. Quick's five recovery times were **1.642, 23.774, 1.934,
0.138, 0.133 s**; none was discarded or retried away. These control-clock/probe
locations differ from the new Tailscale experiment.

A separate single Quick restart **changed the hostname**. The old configured
client failed its echo; explicitly updating the hostname and recreating the client
restored a verified 22-byte echo. No automatic stable-hostname recovery time was
claimed; manual controller/configuration time was outside that timed comparison.
Quick's ephemeral URL behavior is documented [Q1]. The Quick batch's temporary
servers, primary IPs and provider SSH key were removed after capture.

## Published Tailscale benchmarks are separate

| Published metric | Reported observation | Scope and limitation |
| --- | --- | --- |
| Direct TCP dataplane goodput [T1] | **13.0 Gbit/s** bare metal; **7.29 Gbit/s** AWS | Vendor's optimized **wireguard-go** benchmark, 2023-04-13; not Funnel, DERP, or SSH file transfer. |
| Relayed TCP goodput [T2] | **2.20 Mbit/s DERP / 27.5 Mbit/s Peer Relay** | One Delhi–Minnesota field report, 2026-01-26; not universal limits or the newer GA implementation's benchmark. |
| Relayed diagnostic ping [T2] | **441–478 ms DERP**, then **306/298 ms Peer Relay** | Different routes, not HTTP/echo percentiles or isolated protocol overhead. |

[T1] used one iperf 3.9 Cubic stream, 10 s without omitted warm-up, 128 KiB blocks,
Ubuntu 22.04, either two i5-12400/25-Gbit/s Mellanox systems on DAC or two AWS
c6i.8xlarge instances in `us-east-2b`. Optimizations were entering v1.40; this was
not a released-v1.40 daemon benchmark. Its 0.035/0.126 ms average pings were
underlay baselines. [T2] used 120 s observations, Chicago DERP versus a residential
Peer Relay, without exact hardware/software, repeats or reverse-direction results;
it predates Peer Relay GA [T4]. Neither source supplies matched measurements for
this VPS pair, nor a speed ranking against the measured products.

## Product constraints remain different

| Property | FRP tunnel | Cloudflare | Tailscale |
| --- | --- | --- | --- |
| Public client | Native TCP clients; explicit IPv4 publications on available ports 1024–65535. Raw TCP does not itself add authentication/TLS. | TCP/SSH required client-side cloudflared in the Quick batch [C4]. Quick HTTPS needed no special client or account [Q1]. | Private connectivity needs Tailscale or an appropriate gateway. Public Funnel requires TLS, ports **443/8443/10000**, and tailnet `*.ts.net` names [T3]. |
| Limits and stability | Operator/backend policy; the matched 8 MiB POST passed. One relay, no automatic multi-relay HA. | Quick: **200 in-flight requests**, no SSE or uptime guarantee, changing hostname [Q1]. Named Tunnel's **4 connections to ≥2 data centers**, up to **25 replicas**, are **not this Quick setup** [C2]. | Funnel has non-configurable bandwidth limits without a numeric rate disclosed in [T3]. Direct mesh, DERP, Peer Relay and Funnel must not be conflated. |
| HTTP request-body limits | 8 MiB verified in both matched batches; 128 MiB in the archived project-only run. Neither establishes a maximum. | General proxied-HTTP plans document **100 MB Free/Pro, 200 MB Business, Enterprise self-service up to 5 GB** [C5]. Do not relabel those as a verified Quick entitlement or an SSH transfer cap. | 8 MiB verified here; no maximum inferred. No comparable numeric Funnel body-size limit established by the reviewed sources. |
| Cost boundary | MIT code; operator pays for relay, egress and administration. | Quick needs no account; named Tunnel connectivity is available at no cost [C6], with optional services/hosting separate. Public-hostname large-file/video policy restrictions still matter [C7]. | Personal is free for **non-commercial** use; business plans are paid [T5]. Funnel is available on all plans [T3]; customer-hosted relay infrastructure still costs money. |

## Earlier project-only evidence

The [original FRP-only records](benchmark-results/2026-10-05-hetzner.json) and
[their original methodology](https://github.com/0xprincess/frp-tunnel/blob/2ca728a8c3a489368cba406fbc86a1e66a00e38d/docs/comparison.md)
remain available. That pair had an Intel relay/AMD origin. Its Internet/SOCKS
workstation transfers, 128 MiB POST and five explicit FRP transport restarts were
**not rerun as matched three-product experiments**. All five old FRP connections
failed after those restarts; that is a different fault from SIGSTOP/SIGCONT.
Do not pool these records with either newer pair or mistake changed FRP numbers
for a software performance improvement.

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
[T6]: https://pkgs.tailscale.com/stable/tailscale_1.102.4_amd64.tgz.sha256
[T7]: https://tailscale.com/docs/reference/tailscale-cli/funnel
[T8]: https://tailscale.com/docs/reference/tailscale-cli/serve
[T9]: https://tailscale.com/docs/how-to/set-up-https-certificates
[T10]: https://github.com/tailscale/tailscale/blob/v1.102.4/ipn/ipnstate/ipnstate.go

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
- [T6 — Pinned Tailscale archive checksum][T6].
- [T7 — Funnel HTTPS/TLS-terminated TCP CLI][T7].
- [T8 — Private Serve TCP/HTTPS CLI][T8].
- [T9 — HTTPS certificates and public CT disclosure][T9].
- [T10 — Pinned peer status fields and routing interpretation][T10].
