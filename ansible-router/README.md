# ImmortalWrt / OpenWrt Fleet Automation with Ansible

Idempotent, Python-free Ansible automation for managing a mixed fleet of **ImmortalWrt** and **OpenWrt** routers and access points. Built around staged execution, quiet `uci -q batch` scripts, and asynchronous network reloads so any device can provision cleanly, online or offline, without configuration drift or premature SSH drops.

## Supported Device Roles

Any device in `inventory.yml` can be configured as one of three roles:

1. **Wired-Only Core Router** (`core_routers` group + `wireless_enabled: false`)
2. **Core Router with Wireless** (`core_routers` group + `wireless_enabled: true`)
3. **Dumb AP** (`dumb_aps` group)

## Architecture & Network Design

| VLAN ID | Interface | Subnet (IPv4) | IPv6 ULA Hint | Purpose | Firewall Policy |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **10** | `lan` | `192.168.10.0/24` | `:10::/64` | Management & Main LAN | Full access to WAN & all VLANs |
| **20** | `lab` | `192.168.20.0/24` | `:20::/64` | Homelab / Servers | WAN access + Port Forwards; isolated from other VLANs except IoT |
| **30** | `guest` | `192.168.30.0/24` | `:30::/64` | Family / Guest | WAN + Lab VLAN access; isolated from Management/IoT |
| **40** | `iot` | `192.168.40.0/24` | `:40::/64` | Local-Only IoT | Isolated from other VLANs; **no internet access** |
| **41** | `iot_pub` | `192.168.41.0/24` | `:41::/64` | Cloud IoT | Has internet access, but isolated from other VLANs |
| - | `wg0` | `192.168.9.0/24` | `:9::/64` | WireGuard VPN | Grouped into `lan` zone (full access) |

### Key Implementation Details

- **Reset-Gated Role Enforcement (`site.yml` & `common`):**
  - Each provisioned device records its active role (`core_router` or `dumb_ap`) in `/etc/ansible_device_role` and registers that path in `/etc/sysupgrade.conf` so firmware upgrades preserve it while `firstboot` wipes it.
  - If a device is moved between `core_routers` and `dumb_aps` in `inventory.yml` without a factory reset, `site.yml` halts immediately in `pre_tasks` with `ROLE_MISMATCH` to prevent residual router state or SSH lockouts.
- **Single-Play Staged Execution (`site.yml`):**
  - **Stage 1 (Base System & Network):** Runs `common`, `core_router`, `dumb_ap`, and `wireless` using 100% built-in OpenWrt/ImmortalWrt packages, configures static DHCP leases (`dhcp_static_leases`) and split-DNS overrides (`dhcp_static_domains`), and disables conflicting pre-installed adblock daemons (`adblock`, `adblock-fast`, `simple-adblock`). During initial provisioning (`bootstrap_ip`), Stage 1 applies base UCI configs immediately so the device transitions from `192.168.1.1` to its VLAN 10 IP (`192.168.10.1` or `192.168.10.<ap_ip_suffix>`) and dials PPPoE WAN before Stage 2.
  - **Stage 2 (Core Router Add-ons with Offline Fallback):** Targets `core_routers` only. If add-on packages are missing, waits up to 40s for `wan` (PPPoE) to report `"up": true` and installs `kmod-wireguard`, `wireguard-tools`, `ddns-scripts`, `ddns-scripts-cloudflare`, `smartdns`, `wget-ssl`, and `ca-bundle` via `apk`/`opkg`. If WAN or package repositories are unreachable (e.g., first boot or isolated offline staging), the `rescue` block safely skips Stage 2 and retains `9.9.9.9` upstream DNS so LAN clients have immediate internet access once cables are connected.
  - **Stage 3 (Atomic UCI Commit):** Resets dynamic anonymous sections, replays all staged `10_*.uci` and `20_*.uci` fragments in numeric order, commits only subsystems whose MD5 checksum changed, and restarts affected services.
- **Per-Port VLAN Tagging (`port_vlans`):** Configured per physical port in `host_vars/<device>.yml` (`pvid` sets `:u*` untagged/PVID ingress+egress; `tagged` sets `:t`). Defaults to `default_pvid_vlan: 10` with all other VLANs tagged if omitted.
- **IPv6 Prefix Segmentation & Upstream ISP Characteristics:**
  - **Single /64 Prefix Constraint & VLAN Segmentation:** The upstream ISP delegates a single `/64` prefix via DHCPv6-PD. Because a `/64` cannot be subdivided further for SLAAC routing across multiple segments, only VLAN 10 (`lan`) receives the public GUA prefix (alongside ULA). All other VLANs (`lab`, `guest`, `iot`, `iot_pub`) are restricted to ULA only via OpenWrt's native `ip6class: "local"` with `ra_default: "0"` router lifetime suppression to prevent dual-stack fallback stalls when reaching the Internet.
  - **ISP RA vs DHCPv6-PD Reliability:** The ISP provides a public SLAAC address via Router Advertisements on the WAN interface (`wan6`) which is stable, while the delegated DHCPv6-PD prefix can sometimes be intermittent or absent upon PPPoE re-connects. Cloudflare DDNS intentionally tracks `wan6` (`ddns_ipv6_interface: "wan6"`) via `ubus call network.interface.wan6 status` to guarantee AAAA records update accurately using the stable WAN RA address, even when downstream prefix delegation is unavailable. Local routing and split-horizon DNS remain operational via ULA (`network.globals.ula_prefix`).
- **IPv4/IPv6 Inbound Rules & Multi-Domain DDNS:** PPPoE `wan` uses `ipv6='1'` paired with an explicit `wan6` (`dhcpv6`) interface with `peerdns='0'`. IPv4 services are exposed via `port_forwards` (`DNAT`), while IPv6 services use port-specific `ipv6_inbound_rules` with negative-netmask host suffixes (e.g., `::10/-64`) to survive dynamic ISP prefix changes. Cloudflare DDNS supports multiple zones and per-subdomain IPv4 (`wan`) / IPv6 (`wan6`) toggles.
- **Split-Horizon DNS & Ad-Blocking:** `dnsmasq` listens on port `53` across all VLANs and `wg0` (`noresolv='1'`, `localuse='1'`) for DHCP, `.home.arpa` local resolution, and static split-DNS overrides (using `ip6: "::"` to suppress public Cloudflare `AAAA`/`HTTPS` leaks via `NODATA`). Once Stage 2 completes, `smartdns` on `127.0.0.1:5353` (Quad9 DoH/DoT by IP + OISD Small & HaGeZi Pro Mini `domain-set` lists via `/etc/smartdns/custom.conf`) seamlessly overrides the `9.9.9.9` fallback in the same commit pass.
- **Externalized Single-File Vault Design:**
  - All sensitive variables (PPPoE credentials, `ula_base`, WireGuard server/peer keys, SSIDs, static leases/domains, port forwards, IPv6 rules, and Cloudflare DDNS tokens) live in a single Ansible-Vault encrypted file outside this repository: `../homelab-iac-private/ansible-router/group_vars/all/vault.yml`
  - Loaded automatically via `vars_files` in `site.yml`.

## Initial Control Node Setup

### 1. Install Dependencies

```bash
# Tools for generating WireGuard keys and mobile QR codes
sudo apt install wireguard-tools qrencode   # or: brew install wireguard-tools qrencode
```

### 2. Configure Secrets (`vault.yml`)

1. Create the sibling private directory and copy the example vault template into it:

   ```bash
   mkdir -p ../homelab-iac-private/ansible-router/group_vars/all
   cp group_vars/all/vault.yml.example ../homelab-iac-private/ansible-router/group_vars/all/vault.yml
   ```

2. Generate WireGuard keys on the control node for the server and each peer:

   ```bash
   # Server keypair
   PRIV=$(wg genkey); echo "Server Priv: $PRIV"; echo "Server Pub:  $(echo "$PRIV" | wg pubkey)"

   # Peer keypair + PSK (run once per client)
   PRIV=$(wg genkey); echo "Peer Priv: $PRIV"; echo "Peer Pub:  $(echo "$PRIV" | wg pubkey)"; echo "Peer PSK:  $(wg genpsk)"
   ```

3. Fill in `../homelab-iac-private/ansible-router/group_vars/all/vault.yml` (PPPoE credentials, `ula_base`, WireGuard server/peer keys, SSIDs/passphrases, `dhcp_static_leases`, `dhcp_static_domains`, `port_forwards`, `ipv6_inbound_rules`, and Cloudflare DDNS domains/tokens) and encrypt it:

   ```bash
   ansible-vault encrypt ../homelab-iac-private/ansible-router/group_vars/all/vault.yml
   ```

   *(Always use `ansible-vault edit ../homelab-iac-private/ansible-router/group_vars/all/vault.yml` for future edits so plaintext secrets are never left on disk.)*

## Preparing & Bootstrapping a Fresh / Reset Router

1. **Set a root password** via LuCI (`http://192.168.1.1`) or serial/telnet (`passwd`) to enable Dropbear SSH.
2. **Verify physical port and radio names** on new hardware models before running Ansible:

   ```bash
   ssh root@192.168.1.1 "ls /sys/class/net && iwinfo"
   ```

   Update `wan_port`, `lan_ports`, `all_ports`, `port_vlans`, and `radios` (`2g` vs `5g`) in `host_vars/<device>.yml` to match the hardware output.
3. **Run the bootstrap script** from the project root (ensure `chmod +x bootstrap_ssh.sh` has been run once):
   ```bash
   ./bootstrap_ssh.sh router-nr3053
   ```

   *What `bootstrap_ssh.sh` does:*
   - Removes any stale `192.168.1.1` entry from `~/.ssh/known_hosts`.
   - Copies your SSH public key (`~/.ssh/id_ed25519.pub` by default, or override with `PUBKEY=/path/to/key.pub`) into `/etc/dropbear/authorized_keys` with `0600` permissions.
   - Runs `ansible-playbook site.yml --limit <device> -e "bootstrap_ip=192.168.1.1" --ask-vault-pass`. Stage 1 automatically transitions the device from `192.168.1.1` to its static VLAN 10 management IP (`192.168.10.1` for Core Router or `192.168.10.<ap_ip_suffix>` for Dumb AP) mid-play.
   - Pass a second argument to override the factory IP if needed (e.g., `./bootstrap_ssh.sh router-32x6v1 192.168.1.1`).
   - You can run this against an isolated, unplugged router and connect the WAN/trunk cables afterward.

## Running Playbooks

### Routine Fleet Updates

Once a device has been bootstrapped onto VLAN 10, run `site.yml` directly without `bootstrap_ip`:

```bash
# Apply changes to all devices
ansible-playbook site.yml --ask-vault-pass

# Apply changes to a single device
ansible-playbook site.yml --limit router-nr3053 --ask-vault-pass
```

*(Tip: Store your vault password in `~/.ansible_vault_pass` with `chmod 0600` and pass `--vault-password-file ~/.ansible_vault_pass` instead of `--ask-vault-pass`.)*

### Manual Initial Provisioning (If SSH Key Is Already Installed)

If `/etc/dropbear/authorized_keys` is already present on a `192.168.1.1` device, invoke `ansible-playbook` directly:

```bash
ansible-playbook site.yml --limit router-32x6v1 -e "bootstrap_ip=192.168.1.1" --ask-vault-pass
```

## Role-Switching Guide (Reset-Gated)

Every device stamps its active role (`core_router` or `dumb_ap`) into `/etc/ansible_device_role` (preserved across `sysupgrade`, wiped by `firstboot`). If a device's inventory group changes without a factory reset, `site.yml` halts immediately with `ROLE_MISMATCH` to prevent residual router state or SSH lockouts.

### 1. Toggle Wi-Fi On/Off on the Core Router (In-Place)

Changing `wireless_enabled` is not a role change and runs in-place without a reset. Edit `host_vars/router-nr3053.yml` and set `wireless_enabled: false` (Wired-Only) or `true` (Core + Wireless), then run:

```bash
ansible-playbook site.yml --limit router-nr3053 --ask-vault-pass
```

### 2. Switch a Device Between `core_routers` and `dumb_aps`

1. **Factory-reset the target device** to clear `/overlay` and `/etc/ansible_device_role`:

   ```bash
   ssh root@<current_ip> "firstboot -y && reboot"
   ```

2. **Update inventory and host variables:**
   - Move the host between `core_routers` and `dumb_aps` in `inventory.yml`.
   - Update `ansible_host` in `host_vars/<device>.yml` (`192.168.10.1` for `core_routers`, or `192.168.10.<ap_ip_suffix>` for `dumb_aps`).

3. **Re-bootstrap from `192.168.1.1`:**
   Connect to the reset device, set a root password if required by the image, and run:

   ```bash
   ./bootstrap_ssh.sh <device> 192.168.1.1
   ```

## WireGuard Client Profiles

Running `site.yml` against `core_routers` automatically renders ready-to-import client `.conf` files into `./wireguard_clients/` (git-ignored).

- **Desktop import:** Import `wireguard_clients/<peer>.conf` directly into the WireGuard app.
- **Mobile QR code:**

  ```bash
  qrencode -t ansiutf8 < wireguard_clients/phone.conf
  ```

---

## Useful Verification & Troubleshooting Commands

Run these over SSH on a target device to verify state after a playbook run:

```bash
# Check active provisioned role stamp
cat /etc/ansible_device_role

# Check per-port VLAN bridge membership (u = untagged, t = tagged, * = PVID)
bridge vlan show

# Check WAN PPPoE & IPv6 status
ifstatus wan && ifstatus wan6

# Check WireGuard active peers and handshakes
wg show

# Check dnsmasq (port 53), SmartDNS (127.0.0.1:5353), .home.arpa resolution, and ad-blocking
netstat -tlulpn | grep -E '53|smartdns|dnsmasq'
nslookup router-nr3053.home.arpa 127.0.0.1
nslookup doubleclick.net 127.0.0.1

# Check Cloudflare DDNS logs
logread -e ddns
```

---

## QEMU Test Lab & Upstream PPPoE / IPv6 Simulation

The repository includes a rootless QEMU-based test environment in `test_lab/` to validate playbook execution, the Reset-Gate, and offline-to-online transitions without physical hardware.

### Architecture & Features
- **Rootless L2 Multicast Mesh:** Uses QEMU UDP multicast (`230.0.0.1:10001` for WAN, `:10002` for LAN trunk) without requiring `sudo`, TAP devices, or kernel bridges.
- **Mock PPPoE Access Concentrator:** `test_lab/mock_pppoe.py` runs an unprivileged userspace PPPoE AC over multicast, performing PPPoE discovery, PAP authentication (`lab_test_user`/`lab_test_pass`), IPCP dynamic address assignment (`100.64.0.2`), and periodic ICMPv6 Router Advertisements for `wan6` SLAAC (MTU 1492, MSS clamping 1452).
- **Unencrypted Fixture (`vault_mock.yml`):** Supplies syntactically valid Curve25519 keys, mock PPPoE credentials, split-horizon domains, and mock Cloudflare tokens for password-free lab runs (`-e vault_file=test_lab/vault_mock.yml`).
- **Mock Cloudflare API & DNS:** `test_lab/mock_isp.py` verifies DDNS update requests and DNS blocklisting.

### Running the Test Suite

```bash
# 1. Fetch OpenWrt or ImmortalWrt armsr/armv8 lab images (downloads once, injects SSH key)
./test_lab/fetch_image.sh openwrt
# or specify flags: ./test_lab/fetch_image.sh --flavor immortalwrt --version 25.12.2 --arch armsr/armv8

# 2. Run the scenario test matrix in dry-run mode
python3 test_lab/run_matrix.py --dry-run

# 3. Launch mock ISP services (PPPoE, DNS, Cloudflare API)
python3 test_lab/mock_isp.py
```
