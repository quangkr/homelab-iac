# AGENTS.md - OpenWrt / ImmortalWrt Fleet Automation Guide

Architectural rules, hardware constraints, and conventions for AI assistants working on ansible-router.

## 1. Repository & Secrets Topology

- Public repository: `ansible-router/`
  - Playbooks, roles, inventory, and templates. Zero plaintext secrets or client `.conf` files.
- Private repository: `../homelab-iac-private/ansible-router/`
  - `group_vars/all/vault.yml`: Single Ansible-Vault file holding all secrets (PPPoE, WireGuard keys, Wi-Fi PSKs, Cloudflare tokens, static leases/domains, port forwards).
  - `wireguard_clients/`: Plaintext rendered client WireGuard `.conf` profiles.
  - All plaintext secrets must be git-ignored.

## 2. Hard Architectural Invariants (Do Not Violate)

1. Zero-Python Target Execution:
   - Target routers run OpenWrt / ImmortalWrt without Python.
   - Never use Python-dependent Ansible modules (`template`, `copy`, `lineinfile`, `package`, `service`, `setup`).
   - Use strictly `raw` (POSIX `/bin/sh`, `uci`, `ubus`, `awk`, `md5sum`) or `include_tasks: "{{ playbook_dir }}/tasks/deploy_template.yml"` (renders Jinja2 on localhost, streams via SSH stdin).
2. Reset-Gated Role Model (No In-Place Role Switching):
   - Devices belong strictly to one group: `core_routers` OR `dumb_aps`.
   - `roles/common` stamps `{{ device_role }}` into `/etc/ansible_device_role` (persisted in `/etc/sysupgrade.conf`).
   - `site.yml` `pre_tasks` halts with `ROLE_MISMATCH` if role disagrees. Switching roles requires `firstboot -y && reboot` then `./bootstrap_ssh.sh HOST 192.168.1.1`.
   - No bidirectional teardown hacks. Core router `wireless_enabled` toggle is supported in-place.
3. 3-Stage Execution & Numeric `.uci` Fragment Ordering:
   - Roles never mutate `/etc/config/*` directly; render fragments into `/etc/ansible_uci/`:
     - Stage 1: `10_10_common.uci`, `10_20_{core_router|dumb_ap}.uci`, `10_30_wireless.uci`. On `bootstrap_ip`, Stage 1 applies fragments immediately, switches `ansible_host` to VLAN 10 (`192.168.10.1` or `.<ap_ip_suffix>`), and waits for SSH.
     - Stage 2 (`core_routers` only): Waits up to 40s for `ubus call network.interface.wan status` (`"up": true`) if packages missing; installs packages; renders `20_10_wireguard.uci`, `20_20_smartdns.uci`, `20_30_ddns.uci`. On offline WAN, `rescue` deletes `20_*.uci` so `dnsmasq` retains `9.9.9.9` fallback.
     - Stage 3: Snapshots per-file `md5sum` of `/etc/config/*`, purges dynamic anonymous sections (`firewall.@{defaults,zone,forwarding,rule,redirect}`, `network.@device`, `dhcp.@{host,domain}`, `wireless.@wifi-iface`, `ddns.@service`), replays `10_*.uci` then `20_*.uci` via `uci -q batch`, commits, and restarts only services whose config MD5 changed.
4. Dual Package Manager Support (`apk` + `opkg`):
   - Always check `command -v apk >/dev/null 2>&1` before falling back to `opkg` (supports OpenWrt 23.05 `opkg` and 24.10+ / snapshots `apk`).
5. Network & Firewall Segmentation:
   - VLAN 10 (`lan`, `192.168.10.0/24`): Management + full access (includes `wg0`, `192.168.9.0/24`). Only VLAN with GUA + ULA (ISP delegates single `/64`).
   - VLAN 20 (`lab`, `.20.0/24`): WAN + `iot` only. ULA only (`ip6class: 'local'`, `ra_default '0'`).
   - VLAN 30 (`guest`, `.30.0/24`): WAN + `lab` only. ULA only (`ip6class: 'local'`, `ra_default '0'`).
   - VLAN 40 (`iot`, `.40.0/24`): Isolated, no WAN. ULA only (`ip6class: 'local'`, `ra_default '0'`).
   - VLAN 41 (`iot_pub`, `.41.0/24`): WAN only, isolated. ULA only (`ip6class: 'local'`, `ra_default '0'`).
   - No blanket `wan -> lab` forwarding. IPv4 uses `redirect` (DNAT); IPv6 uses port `rule` with negative-netmask suffix (`::10/-64`).
6. Split-Horizon DNS & Leak Prevention:
   - `dnsmasq` owns port 53 (`noresolv='1'`, `localuse='1'`). Static domains with `ip6: "::"` add `server=/<fqdn>/` returning `NODATA` for AAAA/HTTPS (type 65) to prevent public Cloudflare leaks.
   - `smartdns` runs on `127.0.0.1:5353` (`auto_set_dnsmasq='0'`) with Quad9 DoH/DoT + domain-set blocklists.
7. Upstream ISP IPv6 Characteristics & DDNS:
   - Single `/64` DHCPv6-PD delegation restricts public GUA to VLAN 10; other VLANs use private ULA.
   - WAN RA SLAAC on `wan6` is stable; DHCPv6-PD prefix can be intermittent across reconnects.
   - DDNS tracks `wan6` (`ip_source='network'`) via `ubus call network.interface.wan6 status`. Local routing/split-DNS relies on stable ULA (`network.globals.ula_prefix`).
8. Upstream PPPoE WAN & Framing:
   - WAN MTU 1492 due to 8-byte PPPoE encapsulation. Mandatory firewall MSS clamping (`mtu_fix '1'`, clamped to 1452).
   - `wan6` binds to `@wan` (`device '@wan'`) over dynamic `pppoe-wan`.
   - Simulated in test lab via rootless userspace PPPoE AC (`test_lab/mock_pppoe.py`) on multicast `230.0.0.1:10001`.

## 3. Fleet Hardware & Driver Quirks

- `router-nr3053` (Viettel NR3053 - MediaTek MT7981, `aarch64`): MediaTek SDK wifi names (`MT7981_1_1` / `phy: ra0` 2.4 GHz, `MT7981_1_2` / `phy: rax0` 5 GHz), switch ports `wan`, `lan1..lan3`.
- `router-32x6v1` (Viettel VAP 32x6v1 - MediaTek MT7981, `aarch64`): MT7981 CPU, provisional ports `eth0..eth2`.
- `router-cr6606` (Xiaomi CR6606 - MediaTek MT7621, `mipsel_24kc`): Standard `mac80211` (`radio0`, `radio1`), ports `eth0..eth2`.

## 4. Workflow & Hygiene Rules

- Session sync: Read `PROGRESS.md` at session start; update `PROGRESS.md` before finishing tasks. Track only work progress, test verification status, and backlog; never duplicate architectural invariants or configs already documented in `AGENTS.md` or `README.md`.
- Input sanitization: Sanitize dynamic identifiers (DHCP hostnames) with `regex_replace` before writing `uci batch`.
- Secret hygiene: Use `no_log: true` on any task handling credentials, private keys, or API tokens.
- Commit Message Delta Purity: Commit messages must strictly cover the net diff against `HEAD`. Never include transient scaffolding, intermediate variables, or failed/reverted experiments.
- Documentation Parity with Git State: Documentation (`AGENTS.md`, `README.md`, `PROGRESS.md`) must reflect only the clean final state, pruning obsolete/transient details.
- Terminal Readability: Documentation must remain fully readable in terminal pagers (`glow`, `bat`, `cat`, `less`). Avoid unescaped HTML angle brackets outside backticks, avoid bash redirection hazards in command blocks (use uppercase placeholders like `DEVICE`/`IP` or concrete examples), avoid fragile unicode glyphs (arrows, em-dashes, box drawing), and rely on terminal auto-wrapping rather than artificial hard line breaks.
- Token Optimization: Keep `AGENTS.md` and `PROGRESS.md` concise, dense, and high-signal to minimize token consumption. Enforce strict single-responsibility across docs to prevent cross-file duplication while strictly preserving all architectural invariants, hardware quirks, and configuration details.
- File Manifest: Store the repository file list in `TREE.md` (never in `AGENTS.md`). Read `TREE.md` when surveying repo structure without shell commands (e.g. in read-only subagents). Update `TREE.md` whenever adding, deleting, or renaming tracked files via: `git ls-files > TREE.md`
