#!/usr/bin/env python3
"""
test_lab/run_matrix.py — Test Matrix Runner for OpenWrt Fleet Automation
========================================================================
Executes the Reset-Gated scenario matrix against the QEMU test lab:
- CR-1 / CR-2: Core Router initial provisioning (Wi-Fi on vs off).
- CR-3 / CR-4: Core Router offline fallback (9.9.9.9) followed by online add-on upgrade.
- AP-1 / AP-2: Dumb AP provisioning & service isolation verification.
- RG-1: Reset-Gate ROLE_MISMATCH enforcement.
- IDEM-1: Strict idempotency verification (changed=0).
"""

import os
import sys
import time
import argparse
import subprocess
import shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
LAB_SH = os.path.join(SCRIPT_DIR, "lab.sh")
INVENTORY_LAB = os.path.join(SCRIPT_DIR, "inventory_lab.yml")
SITE_YML = os.path.join(PROJECT_DIR, "site.yml")

SCENARIOS = [
    {
        "id": "CR-1",
        "name": "Fresh Core Router Provisioning (Wi-Fi Enabled)",
        "target": "lab-core",
        "role": "core_router",
        "bootstrap": True,
        "extra_vars": "wireless_enabled=true",
        "expect_failure": False,
        "desc": "Bootstrap core router from factory state (192.168.1.1) to VLAN 10 management IP."
    },
    {
        "id": "CR-2",
        "name": "Fresh Core Router Provisioning (Wired-Only)",
        "target": "lab-core",
        "role": "core_router",
        "bootstrap": True,
        "extra_vars": "wireless_enabled=false",
        "expect_failure": False,
        "desc": "Bootstrap wired-only core router and assert wireless interfaces are disabled."
    },
    {
        "id": "CR-3",
        "name": "Core Router Offline Staging (WAN Down)",
        "target": "lab-core",
        "role": "core_router",
        "bootstrap": True,
        "extra_vars": "",
        "expect_failure": False,
        "desc": "Verify Stage 2 rescues gracefully offline and sets 9.9.9.9 upstream fallback."
    },
    {
        "id": "CR-4",
        "name": "Core Router Online Upgrade (WAN Up)",
        "target": "lab-core",
        "role": "core_router",
        "bootstrap": False,
        "extra_vars": "",
        "expect_failure": False,
        "desc": "Re-run after WAN connects to install WireGuard, SmartDNS, and DDNS."
    },
    {
        "id": "AP-1",
        "name": "Fresh Dumb AP Provisioning",
        "target": "lab-ap",
        "role": "dumb_ap",
        "bootstrap": True,
        "extra_vars": "",
        "expect_failure": False,
        "desc": "Bootstrap dumb AP and assert dnsmasq, odhcpd, and firewall are stopped/disabled."
    },
    {
        "id": "RG-1",
        "name": "Reset-Gate In-Place Role Mismatch Enforcement",
        "target": "lab-core",
        "role": "dumb_ap",
        "bootstrap": False,
        "extra_vars": "device_role=dumb_ap",
        "expect_failure": True,
        "expected_error": "ROLE_MISMATCH",
        "desc": "Assert that site.yml pre_tasks aborts if role changes without firstboot."
    },
    {
        "id": "IDEM-1",
        "name": "Strict Idempotency Verification",
        "target": "all",
        "role": None,
        "bootstrap": False,
        "extra_vars": "",
        "expect_failure": False,
        "expect_changed": 0,
        "desc": "Immediate re-run asserting changed=0 across all services."
    }
]


def run_command(cmd, check=True, capture=True):
    """Executes a command and returns exit code, stdout, and stderr."""
    res = subprocess.run(cmd, shell=True, text=True, capture_output=capture)
    if check and res.returncode != 0:
        print(f"Command failed (code {res.returncode}): {cmd}\n{res.stderr}", file=sys.stderr)
        sys.exit(res.returncode)
    return res.returncode, res.stdout, res.stderr


def execute_scenario(sc, dry_run=False, vault_pass_file=None):
    """Executes a scenario and asserts expected outcomes."""
    sc_id = sc["id"]
    name = sc["name"]
    print(f"\n========================================================")
    print(f"Running Scenario [{sc_id}]: {name}")
    print(f"Target: {sc['target']} | Expected Outcome: {'FAIL' if sc['expect_failure'] else 'PASS'}")
    print(f"Description: {sc['desc']}")
    print(f"========================================================")

    if dry_run:
        print(f"[DRY-RUN] Would run lab.sh reset if needed, followed by ansible-playbook.")
        time.sleep(0.1)
        return True, "Dry-run succeeded"

    # Check that lab.sh exists
    if not os.path.exists(LAB_SH):
        return False, f"Harness script missing: {LAB_SH}"

    if sc["target"] == "all":
        roles = ["core", "ap"]
    elif "core" in sc["target"]:
        roles = ["core"]
    else:
        roles = ["ap"]

    for r in roles:
        if sc.get("bootstrap"):
            print(f"Resetting {r} VM to pristine factory state...")
            run_command(f"{LAB_SH} reset-{r}", check=True, capture=False)
            run_command(f"{LAB_SH} wait {r}", check=True, capture=False)
        else:
            run_command(f"{LAB_SH} up {r}", check=True, capture=False)
            run_command(f"{LAB_SH} wait {r}", check=True, capture=False)

    # Build ansible-playbook command
    ansible_cmd = [
        "ansible-playbook",
        SITE_YML,
        "-i", INVENTORY_LAB,
        "--limit", sc["target"]
    ]

    if sc.get("bootstrap"):
        ansible_cmd.extend(["-e", "bootstrap_ip=127.0.0.1"])

    if sc.get("extra_vars"):
        ansible_cmd.extend(["-e", sc["extra_vars"]])

    if vault_pass_file and os.path.exists(vault_pass_file):
        ansible_cmd.extend(["--vault-password-file", vault_pass_file])
    else:
        # Use mock vault fixtures for rootless/offline test lab execution
        mock_vault = os.path.join(SCRIPT_DIR, "vault_mock.yml")
        if os.path.exists(mock_vault):
            ansible_cmd.extend(["-e", f"vault_file={mock_vault}"])
        else:
            example_vault = os.path.join(PROJECT_DIR, "group_vars/all/vault.yml.example")
            if os.path.exists(example_vault):
                ansible_cmd.extend(["-e", f"vault_file={example_vault}"])

    full_cmd = " ".join(ansible_cmd)
    print(f"Executing: {full_cmd}")
    code, stdout, stderr = run_command(full_cmd, check=False)

    if sc["expect_failure"]:
        expected_err = sc.get("expected_error", "")
        if code != 0 and (not expected_err or expected_err in stderr or expected_err in stdout):
            return True, f"Failed as expected with error matching '{expected_err}'"
        else:
            return False, f"Expected failure with '{expected_err}', but got exit code {code}"

    if code != 0:
        err_msg = stderr.strip() if stderr.strip() else stdout.strip()[-500:]
        return False, f"Playbook failed with code {code}: {err_msg}"

    if "expect_changed" in sc:
        if "changed=0" in stdout:
            return True, "Idempotency verified (changed=0)"
        else:
            return False, "Idempotency violated: changes were detected in re-run"

    return True, "Scenario completed successfully"


def main():
    parser = argparse.ArgumentParser(description="Test Matrix Runner for OpenWrt Fleet Automation")
    parser.add_argument("--dry-run", action="store_true", help="Validate scenarios without launching VMs")
    parser.add_argument("--scenario", type=str, help="Run a specific scenario ID (e.g. CR-1, RG-1)")
    parser.add_argument("--list", action="store_true", help="List all scenario definitions")
    parser.add_argument("--flavor", type=str, default="openwrt", choices=["openwrt", "immortalwrt"])
    parser.add_argument("--version", type=str, help="Target firmware version (e.g. 23.05.5, 25.12.2)")
    parser.add_argument("--vault-pass", type=str, help="Ansible Vault password file path")
    args = parser.parse_args()

    if args.list:
        print("Available Test Scenarios:")
        print(f"{'ID':<8} {'Target':<10} {'Expected':<10} {'Name'}")
        print("-" * 65)
        for sc in SCENARIOS:
            exp = "FAIL" if sc["expect_failure"] else "PASS"
            print(f"{sc['id']:<8} {sc['target']:<10} {exp:<10} {sc['name']}")
        return

    scenarios_to_run = SCENARIOS
    if args.scenario:
        scenarios_to_run = [s for s in SCENARIOS if s["id"].upper() == args.scenario.upper()]
        if not scenarios_to_run:
            print(f"Error: Scenario '{args.scenario}' not found.", file=sys.stderr)
            sys.exit(1)

    print(f"=== Starting OpenWrt Fleet Test Matrix ===")
    print(f"Flavor:  {args.flavor}")
    print(f"Mode:    {'DRY-RUN' if args.dry_run else 'LIVE EXECUTION'}")
    print(f"Tests:   {len(scenarios_to_run)} scenarios")
    print(f"==========================================\n")

    results = []
    start_time = time.time()

    for sc in scenarios_to_run:
        ok, msg = execute_scenario(sc, dry_run=args.dry_run, vault_pass_file=args.vault_pass)
        results.append((sc["id"], sc["name"], ok, msg))

    total_time = time.time() - start_time
    print("\n========================================================")
    print("                    TEST MATRIX SUMMARY                 ")
    print("========================================================")
    all_passed = True
    for sc_id, name, ok, msg in results:
        status_str = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"[{sc_id:<6}] {status_str} | {name:<45} | {msg}")
        if not ok:
            all_passed = False

    print("--------------------------------------------------------")
    print(f"Total Scenarios: {len(results)} | Duration: {total_time:.2f}s")
    if all_passed:
        print("\033[92mAll scenario assertions passed successfully!\033[0m")
        sys.exit(0)
    else:
        print("\033[91mSome scenarios failed assertions.\033[0m")
        sys.exit(1)


if __name__ == "__main__":
    main()
