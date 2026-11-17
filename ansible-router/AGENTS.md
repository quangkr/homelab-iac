# AGENTS.md — OpenWrt / ImmortalWrt Fleet Automation Guide

This file provides architectural rules, hardware constraints, and conventions for AI assistants working on `ansible-router`.

## 1. Repository & Secrets Topology

```text
git_root/
├── ansible-router/                        # This repository
│   ├── AGENTS.md                          # Architectural guardrails & coding rules
│   ├── PROGRESS.md                        # Milestones, active test-lab work & backlog
│   ├── README.md                          # User setup & operations manual
│   ├── ansible.cfg                        # Dropbear-compatible SSH/SCP settings
│   ├── bootstrap_ssh.sh                   # First-boot SSH key injector + playbook runner
│   ├── inventory.yml                      # Fleet inventory (core_routers vs dumb_aps)
│   ├── site.yml                           # Single-play, 3-stage, reset-gated playbook
│   ├── group_vars/
│   │   ├── all/main.yml                   # Non-secret VLAN, subnet & blocklist definitions
│   │   ├── all/vault.yml.example          # Template for all encrypted variables
│   │   ├── core_routers.yml               # device_role: core_router, ip_suffix: 1
│   │   └── dumb_aps.yml                   # device_role: dumb_ap, wireless_enabled: true
│   ├── host_vars/                         # Per-device ports, radios & ap_ip_suffix
│   ├── roles/                             # common, core_router, dumb_ap, wireless, wireguard, ddns, smartdns
│   └── tasks/deploy_template.yml          # Python-free SSH stdin template uploader
└── homelab-iac-private/                   # External private repository
    └── ansible-router/
        └── group_vars/all/vault.yml       # Single Ansible-Vault encrypted secrets file
```

- **Secrets Rule:** Never create or reference `local_secrets.yml`. All secrets (PPPoE, WireGuard keys, Wi-Fi keys, Cloudflare API tokens, static leases/domains, port forwards) reside in `../homelab-iac-private/ansible-router/group_vars/all/vault.yml` and are loaded via `vars_files` in `site.yml`.

## 2. Hard Architectural Invariants (Do Not Violate)

1. **Zero-Python Target Execution:**
   - Target devices run OpenWrt / ImmortalWrt without Python.
   - Never use Python-dependent Ansible modules (`template`, `copy`, `lineinfile`, `package`, `service`, `setup`) against target routers.
   - Use strictly `raw` (POSIX `/bin/sh`, `uci`, `ubus`, `awk`, `md5sum`) or `include_tasks: "{{ playbook_dir }}/tasks/deploy_template.yml"` (which renders Jinja2 on `localhost` and streams via `ssh ... 'cat > dest.tmp'`).
2. **Reset-Gated Role Model (No In-Place Role Switching):**
   - Devices belong to exclusively one group: `core_routers` OR `dumb_aps`.
   - `roles/common` writes `{{ device_role }}` to `/etc/ansible_device_role` and adds that path to `/etc/sysupgrade.conf`.
   - `site.yml` `pre_tasks` halts with `ROLE_MISMATCH` if `/etc/ansible_device_role` disagrees with `{{ device_role }}`. Switching roles requires `firstboot -y && reboot` followed by `./bootstrap_ssh.sh <host> 192.168.1.1`.
   - Do **not** re-introduce bidirectional teardown hacks (`wg0`/`wan`/`dnsmasq` cleanup in `dumb_ap`). Toggling `wireless_enabled` on `core_router` *is* supported in-place.
3. **3-Stage Execution & Numeric `.uci` Fragment Ordering:**
   - Roles never mutate `/etc/config/*` directly; they render `.uci` batch fragments into `/etc/ansible_uci/`:
     - `10_10_common.uci` (`common`)
     - `10_20_core_router.uci` (`core_router`) or `10_20_dumb_ap.uci` (`dumb_ap`)
     - `10_30_wireless.uci` (`wireless`)
     - `20_10_wireguard.uci` (`wireguard`)
     - `20_20_smartdns.uci` (`smartdns`)
     - `20_30_ddns.uci` (`ddns`)
   - **Stage 1:** Renders `10_*.uci`. Only when `bootstrap_ip` is defined does Stage 1 apply `10_*.uci` immediately, switch `ansible_host` to VLAN 10 (`192.168.10.1` or `192.168.10.<ap_ip_suffix>`), and wait for SSH.
   - **Stage 2 (`core_routers` only):** Waits up to 40s for `ubus call network.interface.wan status` (`"up": true`) if add-on packages are missing, installs packages, and renders `20_*.uci`. On offline/unreachable WAN, `rescue` deletes `20_*.uci` so `dnsmasq` retains its `9.9.9.9` fallback.
   - **Stage 3:** Snapshots per-file `md5sum` hashes of `/etc/config/*`, deletes dynamic anonymous UCI sections (`firewall.@{defaults,zone,forwarding,rule,redirect}`, `network.@device`, `dhcp.@{host,domain}`, `wireless.@wifi-iface`, `ddns.@service`), replays `10_*.uci` then `20_*.uci` via `uci -q batch`, commits, and restarts only services whose config MD5 changed.
4. **Dual Package Manager Support (`apk` + `opkg`):**
   - Always check `command -v apk >/dev/null 2>&1` before falling back to `opkg` so playbooks work across both OpenWrt 23.05 (`opkg`) and 24.10+ / snapshots (`apk`).
5. **Network & Firewall Segmentation:**
   - VLAN 10 (`lan`, `192.168.10.0/24`): Management + full access (includes `wg0`, `192.168.9.0/24`). Only VLAN that receives both GUA and ULA IPv6 prefixes (the upstream ISP delegates a single `/64` prefix, so only one local VLAN can receive public GUA).
   - VLAN 20 (`lab`, `192.168.20.0/24`): WAN + `iot` only. Restricted to ULA only (`ip6class: 'local'`) with `ra_default '0'`.
   - VLAN 30 (`guest`, `192.168.30.0/24`): WAN + `lab` only. Restricted to ULA only (`ip6class: 'local'`) with `ra_default '0'`.
   - VLAN 40 (`iot`, `192.168.40.0/24`): Isolated, **no WAN**. Restricted to ULA only (`ip6class: 'local'`) with `ra_default '0'`.
   - VLAN 41 (`iot_pub`, `192.168.41.0/24`): WAN only, isolated from all other VLANs. Restricted to ULA only (`ip6class: 'local'`) with `ra_default '0'`.
   - Never add blanket `wan -> lab` zone forwarding. IPv4 uses `redirect` (`DNAT`); IPv6 uses port-specific `rule` entries with negative-netmask suffixes (e.g., `::10/-64`).
6. **Split-Horizon DNS & Leak Prevention:**
   - `dnsmasq` owns port `53` (`noresolv='1'`, `localuse='1'`). Static hostnames in `dhcp_static_domains` with `ip6: "::"` add `server=/<fqdn>/` so `dnsmasq` answers `NODATA` for `AAAA` and `HTTPS` (type 65) queries instead of leaking public Cloudflare AAAA records.
   - `smartdns` runs on `127.0.0.1:5353` (`auto_set_dnsmasq='0'`) with Quad9 DoH/DoT and `domain-set` blocklists.
7. **Upstream ISP IPv6 Characteristics & DDNS:**
   - The upstream ISP provides a WAN IPv6 address via Router Advertisements (RA/SLAAC) on `wan6` and delegates a single `/64` prefix via DHCPv6-PD. Because only a single `/64` prefix is delegated, only one VLAN (VLAN 10) can receive a public GUA prefix; all other VLANs use private ULA prefixes (`ip6class: 'local'`).
   - The WAN RA address is reliable, but the DHCPv6-PD prefix can sometimes be intermittent or absent during dial-up renegotiation.
   - For DDNS, `ddns_ipv6_interface` tracks `wan6` (`ip_source='network'`), reading the stable WAN RA IP address via `ubus call network.interface.wan6 status` rather than relying on delegated LAN prefixes.
   - For local subnets, ULA (`network.globals.ula_prefix`) guarantees unbroken local IPv6 routing and split-DNS resolution even when ISP prefix delegation is temporarily absent.

## 3. Fleet Hardware & Driver Quirks

- **`router-nr3053` (Viettel NR3053 — MediaTek MT7981, `aarch64`):** Uses proprietary MediaTek SDK wireless names (`MT7981_1_1` / `phy: ra0` for 2.4 GHz, `MT7981_1_2` / `phy: rax0` for 5 GHz) and switch port names `wan`, `lan1..lan3`.
- **`router-32x6v1` (Viettel VAP 32x6v1 — MediaTek MT7621, `mipsel_24kc`):** Standard `mac80211` (`radio0`, `radio1`) and `eth0..eth2`.
- **`router-cr6606` (Xiaomi CR6606 — MediaTek MT7621, `mipsel_24kc`):** Standard `mac80211` (`radio0`, `radio1`) and `eth0..eth2`.
- Always sanitize dynamic string identifiers (e.g., DHCP lease hostnames) with `regex_replace` before writing `uci batch` lines.

## 4. Workflow Rules

- Use `no_log: true` on any task handling credentials, private keys, or API tokens.
- At the start of every session, read @PROGRESS.md to understand current task status and blockers.
- Before finishing any coding task, update @PROGRESS.md with completed items and next steps.

