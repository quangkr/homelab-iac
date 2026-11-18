# PROGRESS.md - Development & Testing Status

## Completed Milestones

### Core Automation & Fleet Roles
- Implemented Python-free 3-stage Ansible automation (`site.yml`) for `core_routers` and `dumb_aps`.
- Built and integrated roles: `common`, `core_router`, `dumb_ap`, `wireless`, `wireguard`, `ddns`, `smartdns`.
- Enforced Reset-Gated role isolation (`/etc/ansible_device_role`) preventing dirty in-place role switches.
- Externalized all secrets to single encrypted vault (`group_vars/all/vault.yml`) and external client WireGuard configs.

### QEMU Test Lab & Verification Status
- Built rootless test lab (`test_lab/`) supporting OpenWrt 23.05.5 and ImmortalWrt 25.12.2 across `armsr/armv8` and `malta/le`.
- Automated userspace PPPoE AC (`mock_pppoe.py`), mock DNS/Cloudflare API (`mock_isp.py`), dual-netdev SLIRP isolation, and test runner (`run_matrix.py`).
- Verified live test matrix scenarios:
  - `CR-1` (Core router factory bootstrap to VLAN 10): PASS
  - `CR-2` (Offline staging with 9.9.9.9 fallback): PASS
  - `CR-3` (Online staging with PPPoE WAN & package install): PASS
  - `AP-1` (Dumb AP bootstrap to VLAN 10): PASS
  - `RG-1` (Reset-Gate `ROLE_MISMATCH` halt on dirty role change): PASS
  - `IDEM-1` (Strict idempotency `changed=0` on rerun): PASS

## Backlog

- [x] Security and edge-case audit across roles (`flow_offloading` with WG, IPv6 RA timers, `/etc/sysupgrade.conf`).
- [x] Build rootless VM test lab components (`fetch_image.sh`, `lab.sh`, `inventory_lab.yml`, `mock_isp.py`, `run_matrix.py`, `mock_pppoe.py`, `vault_mock.yml`).
- [ ] Execute full VM test matrix across OpenWrt 23.05.5 and ImmortalWrt 25.12.2 on `armsr-armv8` and `malta-le`.
- [ ] Physical fleet provisioning (`router-nr3053`, `router-32x6v1`, `router-cr6606`), hardware DSA switch VLAN validation (`bridge vlan show`), and 802.11r fast roaming.
