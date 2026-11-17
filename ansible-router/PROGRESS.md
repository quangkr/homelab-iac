# PROGRESS.md — Development & Testing Status

## Completed

- **Core Fleet Architecture & VLAN Segmentation:**
  - Implemented 802.1Q DSA VLAN-filtering bridge (`br-lan`) with per-port `port_vlans` (`pvid` `:u*` and `tagged` `:t`) across VLANs `10` (`lan`), `20` (`lab`), `30` (`guest`), `40` (`iot` offline), and `41` (`iot_pub` internet-only).
  - Hardened `fw4` firewall policy (`REJECT` default input/forward, selective IPv4 `DNAT` port forwards, negative-netmask IPv6 inbound rules like `::10/-64` for dynamic ISP prefixes).
- **Zero-Python Staged Execution (`site.yml` & `tasks/deploy_template.yml`):**
  - Built 3-stage single-play execution using SSH stdin template streaming, ordered `.uci` fragments (`10_*.uci`, `20_*.uci`), anonymous-section resets, per-file `md5sum` change detection, and asynchronous network/Wi-Fi reloads with `wait_for` reconnection.
- **DNS, Ad-Blocking, VPN & DDNS Roles:**
  - `smartdns`: Quad9 DoH/DoT upstreams + OISD Small & HaGeZi Pro Mini `domain-set` ad-blocking on `127.0.0.1:5353`, weekly cron updater, and `9.9.9.9` fallback when offline.
  - `core_router` (`dnsmasq`): Split-horizon DNS with `ip6: "::"` `NODATA` AAAA/HTTPS leak suppression and sanitized static DHCP lease hostnames.
  - `wireguard`: Server + peer UCI config on `wg0` (`192.168.9.0/24`) attached to `lan` zone, plus local client `.conf` rendering in `wireguard_clients/`.
  - `ddns`: Multi-zone Cloudflare IPv4 (`wan`) and IPv6 (`wan6`) DDNS with custom `lookup_host` support.
- **Reset-Gate Role Enforcement & Simplified Secrets:**
  - Added `/etc/ansible_device_role` stamp (persisted in `/etc/sysupgrade.conf`, cleared on `firstboot`) and `pre_tasks` `ROLE_MISMATCH` guard to prevent dirty in-place `core_router` ↔ `dumb_ap` switches.
  - Removed bidirectional teardown workarounds from `site.yml` and `roles/dumb_ap`.
  - Created `bootstrap_ssh.sh` and updated `README.md`.
  - Consolidated `vault.yml` and `local_secrets.yml` into a single encrypted file at `../homelab-iac-private/ansible-router/group_vars/all/vault.yml` loaded via `vars_files`.
- **Fleet Hardening, Guardrails & IPv6 Prefix Segmentation:**
  - Standardized first-boot script to `bootstrap_ssh.sh` (renamed from `bootstrap.sh`).
  - Restricted IPv6 prefix delegation: the ISP delegates a single `/64` DHCPv6-PD prefix, so only VLAN 10 receives public GUA + ULA prefixes; VLANs 20, 30, 40, and 41 are restricted to ULA only (`ip6class: "local"`) with `ra_default '0'` router lifetime suppression to prevent dual-stack Internet stalls.
  - Documented upstream ISP IPv6 characteristics (stable WAN RA SLAAC vs intermittent single `/64` DHCPv6-PD delegation) and DDNS binding architecture.
  - Fixed Dumb AP service reload in `site.yml` Stage 3, guarding `dnsmasq`, `odhcpd`, and `firewall` with `[ "{{ device_role }}" = "core_router" ]` and setting `dhcp.lan.ignore='1'`.
  - Added anonymous `network.@bridge-vlan[0]` purges in Stage 1 and Stage 3 to clean factory default VLAN 1.
  - Added dummy unserved `dhcp.wg0` section in `wireguard` to bind `dnsmasq` without violating `localservice='1'`, and appended `/32` & `/128` CIDR masks to client `.conf` templates.
  - Added package pre-check for `curl` in Stage 2 and `--timeout=30 -t 3` to SmartDNS blocklist downloads with conditional reload.

---

## In Progress — Virtual Machine Test Lab Setup (`test_lab/`)

Building a portable, rootless QEMU test harness capable of running on **Linux desktop (KVM)**, **Windows desktop**, or **Debian `proot-distro` under Android Termux (TCG)** to validate the entire fleet across OpenWrt and ImmortalWrt releases before deploying to physical hardware.

### Planned Test Lab Architecture

- **Userspace L2 Networking:** QEMU UDP multicast switches (`-netdev socket,mcast=230.0.0.1:1001` for WAN, `:1002` for LAN trunk) with `virtio-net-pci` NICs (preserves 802.1Q VLAN tags without host root/TAP interfaces).
- **Guest Targets Matching Fleet ISAs:**
  - `armsr/armv8` (`qemu-system-aarch64 -M virt -cpu cortex-a53`) — matches `router-nr3053` (MT7981).
  - `malta/le` (`qemu-system-mipsel -M malta -cpu 24Kc`) — matches `router-32x6v1` & `router-cr6606` (MT7621).
  - Selectable `OPENWRT_FLAVOR` (`openwrt` / `immortalwrt`), `OPENWRT_VERSION` (`23.05.5`, `24.10.0`), and `OPENWRT_ARCH` backed by Copy-on-Write `qcow2` overlays for <1s factory resets.
- **ISP & Client Harness VM (`isp-and-client-harness`):**
  - `netns isp` (`eth1`): `rp-pppoe` server (PAP/CHAP), DHCPv6-PD server delegating dynamic `/56` prefixes, mock Quad9 (`9.9.9.9` UDP/53 + DoT/853 + DoH/443), mock Cloudflare DDNS API (`104.16.132.229` logging payloads), and WireGuard remote peer (`192.168.9.2`).
  - `netns client` (`eth2.{10,20,30,40,41}`): Automated DHCPv4/v6, DNS split-horizon/ad-block, and inter-VLAN + WAN inbound firewall probe runner.

### Test Lab Implementation Checklist

- [ ] `test_lab/fetch_image.sh`: Download OpenWrt/ImmortalWrt kernel + ext4 rootfs, resize rootfs to 256 MB, and inject Dropbear SSH public key offline via `e2fsprogs` `debugfs`.
- [ ] `test_lab/isp_client_setup.sh`: Provision `isp` and `client` network namespaces, PPPoE server, DHCPv6-PD prefix rotation, mock Quad9 & Cloudflare API endpoints, and WireGuard test peer.
- [ ] `test_lab/lab.sh`: Orchestrate VMs (`up`, `reset-core`, `reset-ap`, `wan-up`, `wan-down`, `down`) using ephemeral `qcow2` snapshots.
- [ ] `test_lab/inventory_lab.yml`: Map QEMU `eth0..3` and `radio0..1` (`mac80211_hwsim`) to `core_routers` and `dumb_aps`.
- [ ] `test_lab/run_matrix.py`: Execute the Reset-Gated test matrix and assertions:
  - [ ] **CR-1 / CR-2:** Fresh Core Router (`192.168.1.1`) with Wi-Fi enabled vs disabled (`wireless_enabled: false`), PPPoE connected on Run 1.
  - [ ] **CR-3 / CR-4:** Fresh Core Router provisioned offline (`wan-down`), verifying `9.9.9.9` DNS fallback and base routing/firewall before Run 2, followed by `wan-up` + Run 2 upgrade to SmartDNS/WireGuard/DDNS.
  - [ ] **AP-1 / AP-2:** Fresh Dumb AP (`192.168.1.1`) with Core Router trunk connected before vs after Ansible run.
  - [ ] **RG-1:** Verify Reset-Gate halts with `ROLE_MISMATCH` if inventory role changes without `firstboot`, and succeeds after `reset-core`/`reset-ap`.
  - [ ] **IDEM-1:** Re-run `site.yml` immediately after every scenario and assert `changed=0`.

---

## Up Next / Backlog

- [x] Perform full security and edge-case audit across `core_router`, `wireless`, `smartdns`, `wireguard`, and `ddns` roles (e.g., `flow_offloading` interaction with WireGuard, IPv6 RA/DHCPv6 timers, `/etc/sysupgrade.conf` completeness).
- [ ] Build Virtual Machine test lab (`test_lab/`) components (`fetch_image.sh`, `isp_client_setup.sh`, `lab.sh`, `inventory_lab.yml`, `run_matrix.py`).
- [ ] Execute full VM test matrix across OpenWrt `23.05.5`, OpenWrt `24.10.0`, and ImmortalWrt `24.10.0` on both `armsr-armv8` and `malta-le`.
- [ ] Provision production fleet (`router-nr3053`, `router-32x6v1`, `router-cr6606`) and verify hardware DSA switch VLANs (`bridge vlan show`) and 802.11r fast roaming.
