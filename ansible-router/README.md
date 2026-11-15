# ImmortalWrt / OpenWrt Fleet Automation with Ansible

Idempotent, Python-free Ansible automation for managing a mixed fleet of **ImmortalWrt** and **OpenWrt** routers and access points. Built around staged execution, quiet `uci -q batch` scripts, and asynchronous network reloads so any device can cleanly switch roles or provision offline without residual configuration drift or premature SSH drops.

---

## Supported Device Roles

Any device in `inventory.yml` can be switched between three roles:
1. **Wired-Only Core Router** (`core_routers` group + `wireless_enabled: false`)
2. **Core Router with Wireless** (`core_routers` group + `wireless_enabled: true`)
3. **Dumb AP** (`dumb_aps` group)

---

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
- **Single-Play Staged Execution (`site.yml`):**
  * **Stage 1 (Base System & Network):** Runs `common`, `core_router`, `dumb_ap`, and `wireless` using 100% built-in OpenWrt/ImmortalWrt packages, configures static DHCP leases (`dhcp_static_leases`), disables conflicting pre-installed adblock daemons (`adblock`, `adblock-fast`, `simple-adblock`), bootstraps PPPoE WAN early if not yet active, and switches `ansible_host` to the device's target role IP.
  * **Stage 2 (Core Router Add-ons with Offline Fallback):** Targets `core_routers` only. If add-on packages are missing, waits up to 40s for `wan` (PPPoE) to report `"up": true` and installs `wireguard`, `ddns`, and `smartdns` via `apk`/`opkg`. If WAN or package repositories are unreachable (e.g., first boot or isolated offline staging), the `rescue` block safely skips Stage 2 and retains `9.9.9.9` upstream DNS so LAN clients have immediate internet access once cables are connected.
  * **Stage 3 (Atomic UCI Commit):** Replays all staged `10_*.uci` and `20_*.uci` fragments in a single pass, commits only subsystems whose MD5 checksum changed, and restarts affected services.
- **Per-Port VLAN Tagging (`port_vlans`):** Configured per physical port in `host_vars/<device>.yml` (`pvid` sets `:u*` untagged/PVID ingress+egress; `tagged` sets `:t`). Defaults to `default_pvid_vlan: 10` with all other VLANs tagged if omitted.
- **IPv6 & Multi-Domain DDNS:** PPPoE `wan` uses `ipv6='1'` (Manual) paired with an explicit `wan6` (`dhcpv6`) interface with `peerdns='0'`. Cloudflare DDNS supports multiple zones and per-subdomain IPv4 (`wan`) / IPv6 (`wan6`) toggles.
- **DNS & Ad-Blocking:** `dnsmasq` listens on port `53` across all VLANs and `wg0` (`noresolv='1'`, `localuse='1'`) for DHCP and `.home.arpa` local hostname resolution. Stage 1 configures `9.9.9.9` as a working upstream fallback; once Stage 2 completes, `smartdns` on `127.0.0.1:5353` (Quad9 DoH/DoT by IP + OISD Small & HaGeZi Pro Mini `domain-set` lists via `/etc/smartdns/custom.conf`) seamlessly overrides it in the same commit pass.
- **Split Secrets Design:**
  * `group_vars/all/vault.yml` (Ansible Vault encrypted): PPPoE credentials, `ula_base`, `ssids`, `dhcp_static_leases`, `port_forwards`, `ddns_domains`.
  * `group_vars/all/local_secrets.yml` (git-ignored): WireGuard port, endpoint, server private/public keys and peer definitions/keys so Core Router failover preserves identical VPN keys without cluttering the vault.

---

## Initial Control Node Setup

### 1. Install Dependencies
```bash
# Tools for generating WireGuard keys and mobile QR codes
sudo apt install wireguard-tools qrencode   # or: brew install wireguard-tools qrencode
```

### 2. Configure Secrets (`vault.yml` & `local_secrets.yml`)
1. Copy the example secret files:
   ```bash
   cp group_vars/all/vault.yml.example group_vars/all/vault.yml
   cp group_vars/all/local_secrets.yml.example group_vars/all/local_secrets.yml
   ```
2. Generate WireGuard keys on the control node for the server and each peer:
   ```bash
   # Server keypair
   PRIV=$(wg genkey); echo "Server Priv: $PRIV"; echo "Server Pub:  $(echo "$PRIV" | wg pubkey)"

   # Peer keypair + PSK (run once per client)
   PRIV=$(wg genkey); echo "Peer Priv: $PRIV"; echo "Peer Pub:  $(echo "$PRIV" | wg pubkey)"; echo "Peer PSK:  $(wg genpsk)"
   ```
3. Populate `group_vars/all/local_secrets.yml` (git-ignored) with the WireGuard port, endpoint, server keys and `wireguard_peers` list.
4. Fill in `group_vars/all/vault.yml` (PPPoE credentials, `ula_base`, SSIDs/passphrases, `dhcp_static_leases`, port forwards, Cloudflare DDNS domains/tokens) and encrypt it:
   ```bash
   ansible-vault encrypt group_vars/all/vault.yml
   ```
   *(Always use `ansible-vault edit group_vars/all/vault.yml` for future edits so plaintext secrets are never left on disk.)*

---

## Preparing a Fresh / Reset Router

1. **Set a root password** via LuCI (`http://192.168.1.1`) or serial/telnet (`passwd`) to enable Dropbear SSH.
2. **Copy your SSH public key** into Dropbear's key path:
   ```bash
   ssh-keygen -R 192.168.1.1
   cat ~/.ssh/id_ed25519.pub | ssh root@192.168.1.1 \
     "cat >> /etc/dropbear/authorized_keys && chmod 0600 /etc/dropbear/authorized_keys"
   ```
3. **Verify physical port and radio names** before running Ansible:
   ```bash
   ssh root@192.168.1.1 "ls /sys/class/net && iwinfo"
   ```
   Update `wan_port`, `lan_ports`, `all_ports`, `port_vlans`, and `radios` (`2g` vs `5g`) in `host_vars/<device>.yml` to match the hardware output.

---

## Running Playbooks

### Provision a Brand-New Device (from `192.168.1.1`)
Override `ansible_host` for the first run while the router is still on its factory IP. Stage 1 will automatically switch `ansible_host` to the device's static VLAN 10 IP (`192.168.10.1` or `192.168.10.<ap_ip_suffix>`) mid-play. You can also run this against an isolated, unplugged router and connect the cables afterward:
```bash
ansible-playbook site.yml --limit ap1 -e "bootstrap_ip=192.168.1.1" --ask-vault-pass
```

### Routine Fleet Updates
```bash
# Apply changes to all devices
ansible-playbook site.yml --ask-vault-pass

# Apply changes to a single device
ansible-playbook site.yml --limit main-router --ask-vault-pass
```
*(Tip: Store your vault password in `~/.ansible_vault_pass` with `chmod 0600` and pass `--vault-password-file ~/.ansible_vault_pass` instead of `--ask-vault-pass`.)*

---

## Role-Switching Playbook Guide

### 1. Toggle Wi-Fi On/Off on the Core Router
Edit `host_vars/main-router.yml` and set `wireless_enabled: false` (Wired-Only) or `true` (Core + Wireless), then run:
```bash
ansible-playbook site.yml --limit main-router --ask-vault-pass
```

### 2. Convert a Core Router (`192.168.10.1`) into a Dumb AP
1. Move the host from `core_routers` to `dumb_aps` in `inventory.yml`.
2. Run Ansible while the router is still reachable at `192.168.10.1`:
   ```bash
   ansible-playbook site.yml --limit main-router --ask-vault-pass
   ```
   *This stops/disables `dnsmasq`, `odhcpd`, `firewall`, `ddns`, and `smartdns`, removes `wg0`/`wan`/`wan6`, bridges `all_ports` into `br-lan` using `port_vlans`, and moves management to `192.168.10.<ap_ip_suffix>`.*
3. Update `ansible_host` in `host_vars/main-router.yml` to its new AP IP (`192.168.10.9`) for future runs.

### 3. Promote a Dumb AP (`192.168.10.2`) to Core Router (`192.168.10.1`)
1. Move the host from `dumb_aps` to `core_routers` in `inventory.yml`.
2. Run Ansible while it is still reachable at its AP IP:
   ```bash
   ansible-playbook site.yml --limit ap1 --ask-vault-pass
   ```
   *Stage 1 configures PPPoE WAN (with 9.9.9.9 DNS fallback) and moves the device to 192.168.10.1; Stage 2 waits for WAN to come online and deploys WireGuard, DDNS, and SmartDNS.*
3. Update `ansible_host: 192.168.10.1` in `host_vars/ap1.yml`.

---

## WireGuard Client Profiles

Running `site.yml` against `core_routers` automatically renders ready-to-import client `.conf` files into `./wireguard_clients/` (git-ignored).
* **Desktop import:** Import `wireguard_clients/<peer>.conf` directly into the WireGuard app.
* **Mobile QR code:**
  ```bash
  qrencode -t ansiutf8 < wireguard_clients/phone.conf
  ```

---

## Useful Verification & Troubleshooting Commands

Run these over SSH on a target device to verify state after a playbook run:

```bash
# Check per-port VLAN bridge membership (u = untagged, t = tagged, * = PVID)
bridge vlan show

# Check WAN PPPoE & IPv6 status
ifstatus wan && ifstatus wan6

# Check WireGuard active peers and handshakes
wg show

# Check dnsmasq (port 53), SmartDNS (127.0.0.1:5353), .home.arpa resolution, and ad-blocking
netstat -tlulpn | grep -E '53|smartdns|dnsmasq'
nslookup main-router.home.arpa 127.0.0.1
nslookup doubleclick.net 127.0.0.1

# Check Cloudflare DDNS logs
logread -e ddns
```
