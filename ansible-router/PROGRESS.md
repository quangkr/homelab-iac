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

- **Virtual Machine Test Lab Setup & Production Parity (`test_lab/`):**
  - Created rootless QEMU test lab supporting OpenWrt `23.05.5` and ImmortalWrt `25.12.2` across `armsr/armv8` (matching MT7981 `router-nr3053` & `router-32x6v1`) and `malta/le` (matching MT7621 `router-cr6606`).
  - Built `test_lab/fetch_image.sh` with offline Dropbear SSH key injection via `debugfs` directly on ext4 images without host root.
  - Built `test_lab/lab.sh` orchestrator with userspace UDP multicast L2 networking (`230.0.0.1:10001` WAN, `:10002` LAN trunk) and sub-second ephemeral `qcow2` overlay snapshots.
  - Built `test_lab/inventory_lab.yml` mapping virtio NICs and ports to `core_routers` (`lab-core`) and `dumb_aps` (`lab-ap`).
  - Built `test_lab/mock_isp.py` providing userspace mock DNS and Cloudflare DDNS REST API validation.
  - Built `test_lab/run_matrix.py` implementing the automated Reset-Gated scenario matrix (CR-1..4, AP-1, RG-1, IDEM-1).
  - **100% Production Parity:** Maintained clean production templates (`uci_core.j2`, `uci_dumb_ap.j2`) with zero test-specific logic or conditional branches.
  - **Dual-Netdev QEMU Bootstrap Architecture:** Solved QEMU SLIRP off-link routing constraints cleanly in test harness by mapping two independent userspace management netdevs: `mgmt_factory` (`192.168.1.0/24`, ports `22221`/`22231`) and `mgmt_vlan10` (`192.168.10.0/24`, `host=192.168.10.253` for core / `.254` for ap, ports `22220`/`22230`). This prevents SLIRP virtual gateway collisions (`.2`) and permits real-world bootstrap from factory `192.168.1.1` to VLAN 10 without leaving secondary management IPs on routers.
  - **SIGHUP Immunity & Process Group Detachment:** Added `trap '' HUP INT TERM` in parent shell and background subshells in `site.yml` Stage 1 and Stage 3, preventing Dropbear SSH session closure from aborting network/firewall reloads.
  - **Strict Idempotency (`changed=0`):** Preserved staged `.uci` fragments in `pre_tasks` to allow `tasks/deploy_template.yml` `cmp -s` diffing, verified `changed=0` across both `core_router` and `dumb_ap` on subsequent runs.
  - **Empirical Scenario Verification:** Verified `CR-1` (PASS), `CR-2` (PASS), `CR-3` (PASS), `AP-1` (PASS), `RG-1` (PASS), and `IDEM-1` (PASS) against live QEMU VMs.
- **PPPoE Upstream Emulation & Password-Free Mock Fixtures:**
  - Implemented `test_lab/mock_pppoe.py`: a rootless userspace PPPoE Access Concentrator operating over QEMU multicast (`230.0.0.1:10001`). Handles PPPoE Discovery (`PADI`/`PADO`/`PADR`/`PADS`/`PADT`), PPP LCP negotiation, PAP authentication (`lab_test_user`/`lab_test_pass`), IPCP dynamic IPv4 assignment (`100.64.0.2`), and IPv6CP negotiation with periodic ICMPv6 Router Advertisements for SLAAC WAN IPv6 configuration.
  - Integrated PPPoE server into `test_lab/mock_isp.py` with multi-service threading (`--pppoe-only`, `--dns-only`, `--cf-only`, `--no-pppoe`).
  - Created `test_lab/vault_mock.yml`: unencrypted, syntactically and cryptographically valid mock secrets fixture (valid Curve25519 WireGuard keys, mock PPPoE credentials, split-horizon domains, mock Cloudflare tokens).
  - Pluggable Vault in `site.yml`: enabled `vault_file | default(...)` in `site.yml` and configured `test_lab/inventory_lab.yml` and `test_lab/run_matrix.py` to seamlessly execute playbooks in test lab mode without entering vault passwords or exposing production secrets.
  - Documented PPPoE 8-byte framing overhead (MTU 1492 vs 1500), mandatory TCP MSS clamping (`mtu_fix '1'`), and `@wan` dynamic alias binding in `AGENTS.md` and `README.md`.

- **Comprehensive Codebase & Test Lab Audit (Completed):**
  - **Multicast Port Realignment:** Corrected documentation in `AGENTS.md` and `README.md` to reference rootless unprivileged UDP multicast ports `10001` (WAN) and `10002` (LAN trunk) matching `lab.sh`, `mock_isp.py`, and `mock_pppoe.py`.
  - **CLI Usability & Help:** Updated `test_lab/fetch_image.sh` to support positional flavor arguments (e.g. `./test_lab/fetch_image.sh openwrt`) alongside `--flavor`, corrected `x86_64` to `armsr/armv8` in `README.md`, added `wait` command to `lab.sh` usage help, and updated VM startup logging to report active dual-netdev ports.
  - **Multi-VM SLIRP Collision Resolution:** Discovered and resolved an ARP/FDB collision that occurred when `lab-core` and `lab-ap` VMs ran concurrently over the L2 multicast trunk. Isolated their virtual SLIRP gateways to non-conflicting IP addresses (`192.168.10.253` for core, `192.168.10.254` for ap), eliminating bridge MAC flaps and banner exchange timeouts.
  - **SSH Identity Key Parameterization:** Updated `tasks/deploy_template.yml` to pass `ansible_ssh_private_key_file` (`-i <key>`) when specified, ensuring non-default SSH keys authenticate properly during stdin template streaming.
  - **Jinja2 Template Robustness & Defensive Guards:** Added `| default([])` guards to loops in `uci_wireless.j2`, `uci_ddns.j2`, and `uci_wireguard.j2` so playbook execution succeeds even if optional lists are omitted from `vault.yml`. Guarded `key` in `uci_wireless.j2` to cleanly support open Wi-Fi networks without passwords.
  - **Device Configuration Cleanup:** Removed redundant `delete network.@device[0]` from `uci_common.j2` to prevent unintended deletion of non-bridge devices, relying exclusively on `site.yml`'s pre-replay anonymous purge.
  - **Task Idempotency & Hygiene:** Refactored SmartDNS crontab task in `roles/smartdns/tasks/main.yml` to restart `cron` only when a crontab entry was newly added or cron was stopped. Added `2>/dev/null` to `opkg status` across `wireguard`, `ddns`, and `smartdns` to suppress benign non-installed stderr output.
  - **Empirical Regression Verification:** Verified live scenario runs: `CR-1` (PASS, 135.75s), `RG-1` (PASS, 2.52s), `AP-1` (PASS, 84.94s), and `IDEM-1` (PASS, 63.54s with strict `changed=0`).

---

## Up Next / Backlog

- [x] Perform full security and edge-case audit across `core_router`, `wireless`, `smartdns`, `wireguard`, and `ddns` roles (e.g., `flow_offloading` interaction with WireGuard, IPv6 RA/DHCPv6 timers, `/etc/sysupgrade.conf` completeness).
- [x] Build Virtual Machine test lab (`test_lab/`) components (`fetch_image.sh`, `lab.sh`, `inventory_lab.yml`, `mock_isp.py`, `run_matrix.py`, `mock_pppoe.py`, `vault_mock.yml`).
- [ ] Execute full VM test matrix across OpenWrt `23.05.5` and ImmortalWrt `25.12.2` on both `armsr-armv8` and `malta-le`.
- [ ] Provision production fleet (`router-nr3053`, `router-32x6v1`, `router-cr6606`) and verify hardware DSA switch VLANs (`bridge vlan show`) and 802.11r fast roaming.
