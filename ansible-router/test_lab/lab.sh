#!/usr/bin/env bash
# ==============================================================================
# test_lab/lab.sh — Multi-Arch QEMU VM Test Lab Orchestrator
# ==============================================================================
# Orchestrates rootless QEMU virtual machines for Core Router and Dumb AP
# testing using UDP multicast L2 networking and ephemeral copy-on-write snapshots.
# ==============================================================================
set -euo pipefail
export PATH="/sbin:/usr/sbin:${PATH}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${SCRIPT_DIR}/base_images"
RUN_DIR="${SCRIPT_DIR}/run"
mkdir -p "${RUN_DIR}"

FLAVOR="${FLAVOR:-openwrt}"
VERSION="${VERSION:-23.05.5}"
ARCH="${ARCH:-armsr/armv8}"
ARCH_SLUG="$(echo "${ARCH}" | tr '/' '_')"

BASE_IMG="${BASE_DIR}/${FLAVOR}_${VERSION}_${ARCH_SLUG}.qcow2"
if [ ! -f "${BASE_IMG}" ] && [ -f "${BASE_DIR}/${FLAVOR}_${VERSION}_${ARCH_SLUG}.raw" ]; then
  BASE_IMG="${BASE_DIR}/${FLAVOR}_${VERSION}_${ARCH_SLUG}.raw"
fi
KERNEL_BIN="${BASE_DIR}/${FLAVOR}_${VERSION}_${ARCH_SLUG}_kernel.bin"

WAN_MCAST="230.0.0.1:10001"
LAN_MCAST="230.0.0.1:10002"

CORE_PID_FILE="${RUN_DIR}/core.pid"
AP_PID_FILE="${RUN_DIR}/ap.pid"
CORE_LOG="${RUN_DIR}/core.log"
AP_LOG="${RUN_DIR}/ap.log"

CORE_OVERLAY="${RUN_DIR}/core_overlay.qcow2"
AP_OVERLAY="${RUN_DIR}/ap_overlay.qcow2"

get_qemu_bin() {
  local target_arch="$1"
  if [ "${target_arch}" = "armsr/armv8" ]; then
    echo "qemu-system-aarch64"
  elif [ "${target_arch}" = "malta/le" ]; then
    echo "qemu-system-mipsel"
  elif [ "${target_arch}" = "x86/64" ]; then
    echo "qemu-system-x86_64"
  else
    echo "Unknown target architecture: ${target_arch}" >&2
    exit 1
  fi
}

check_qemu_installed() {
  local qbin
  qbin="$(get_qemu_bin "${ARCH}")"
  if ! command -v "${qbin}" >/dev/null 2>&1; then
    echo "Error: QEMU binary '${qbin}' is not installed or not in PATH." >&2
    echo "Install it via your package manager:" >&2
    echo "  Debian/Ubuntu: sudo apt install qemu-system-arm qemu-system-mips qemu-utils" >&2
    echo "  Termux:        pkg install qemu-system-aarch64 qemu-system-mipsel qemu-utils" >&2
    exit 1
  fi
  if ! command -v qemu-img >/dev/null 2>&1; then
    echo "Error: 'qemu-img' is not installed or not in PATH." >&2
    exit 1
  fi
}

start_vm() {
  local role="$1" # core or ap
  local pid_file
  local log_file
  local overlay
  local ssh_port
  local qbin
  qbin="$(get_qemu_bin "${ARCH}")"

  local port_vlan10
  local port_factory
  if [ "${role}" = "core" ]; then
    pid_file="${CORE_PID_FILE}"
    log_file="${CORE_LOG}"
    overlay="${CORE_OVERLAY}"
    port_vlan10=22220
    port_factory=22221
  else
    pid_file="${AP_PID_FILE}"
    log_file="${AP_LOG}"
    overlay="${AP_OVERLAY}"
    port_vlan10=22230
    port_factory=22231
  fi

  if [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
    echo "${role} VM is already running (PID: $(cat "${pid_file}"))"
    return 0
  fi

  if [ ! -f "${BASE_IMG}" ]; then
    echo "Error: Base image not found at ${BASE_IMG}" >&2
    echo "Run './fetch_image.sh --flavor ${FLAVOR} --version ${VERSION} --arch ${ARCH}' first." >&2
    exit 1
  fi

  # Create ephemeral CoW overlay if missing
  if [ ! -f "${overlay}" ]; then
    echo "Creating ephemeral overlay for ${role}..."
    qemu-img create -f qcow2 -b "${BASE_IMG}" -F qcow2 "${overlay}" >/dev/null
  fi

  echo "Starting ${role} VM (Arch: ${ARCH}, SSH: vlan10=127.0.0.1:${port_vlan10}, factory=127.0.0.1:${port_factory})..."

  local QEMU_ARGS=()
  if [ "${ARCH}" = "armsr/armv8" ]; then
    QEMU_ARGS+=(
      -M virt
      -cpu cortex-a53
      -m 512M
      -smp 2
      -nographic
    )
    if [ -f "${KERNEL_BIN}" ]; then
      QEMU_ARGS+=(
        -kernel "${KERNEL_BIN}"
        -append "root=/dev/vda rootwait console=ttyAMA0,115200"
      )
    fi
  elif [ "${ARCH}" = "malta/le" ]; then
    QEMU_ARGS+=(
      -M malta
      -cpu 24Kc
      -m 256M
      -nographic
    )
    if [ -f "${KERNEL_BIN}" ]; then
      QEMU_ARGS+=(
        -kernel "${KERNEL_BIN}"
        -append "root=/dev/sda rootwait console=ttyS0,115200"
      )
    fi
  fi

  # Drive configuration
  QEMU_ARGS+=(
    -drive "file=${overlay},if=virtio,format=qcow2"
  )

  # Network Topology:
  # eth0: Factory Bootstrap Management (192.168.1.1:22)
  # Core: eth1=WAN multicast, eth2=LAN trunk multicast, eth3=VLAN 10 Management (192.168.10.1:22)
  # AP:   eth1=LAN trunk multicast, eth2=VLAN 10 Management (192.168.10.2:22)
  if [ "${role}" = "core" ]; then
    QEMU_ARGS+=(
      -netdev "user,id=mgmt_factory,net=192.168.1.0/24,host=192.168.1.253,hostfwd=tcp:127.0.0.1:22221-192.168.1.1:22"
      -device "virtio-net-pci,netdev=mgmt_factory,mac=52:54:00:12:34:00"
      -netdev "socket,id=wan,mcast=${WAN_MCAST}"
      -device "virtio-net-pci,netdev=wan,mac=52:54:00:12:34:01"
      -netdev "socket,id=lan,mcast=${LAN_MCAST}"
      -device "virtio-net-pci,netdev=lan,mac=52:54:00:12:34:02"
      -netdev "user,id=mgmt_vlan10,net=192.168.10.0/24,host=192.168.10.253,hostfwd=tcp:127.0.0.1:22220-192.168.10.1:22"
      -device "virtio-net-pci,netdev=mgmt_vlan10,mac=52:54:00:12:34:03"
    )
  else
    QEMU_ARGS+=(
      -netdev "user,id=mgmt_factory,net=192.168.1.0/24,host=192.168.1.254,hostfwd=tcp:127.0.0.1:22231-192.168.1.1:22"
      -device "virtio-net-pci,netdev=mgmt_factory,mac=52:54:00:12:34:10"
      -netdev "socket,id=lan,mcast=${LAN_MCAST}"
      -device "virtio-net-pci,netdev=lan,mac=52:54:00:12:34:11"
      -netdev "user,id=mgmt_vlan10,net=192.168.10.0/24,host=192.168.10.254,hostfwd=tcp:127.0.0.1:22230-192.168.10.2:22"
      -device "virtio-net-pci,netdev=mgmt_vlan10,mac=52:54:00:12:34:12"
    )
  fi

  setsid "${qbin}" "${QEMU_ARGS[@]}" < /dev/null > "${log_file}" 2>&1 &
  echo $! > "${pid_file}"
  echo "${role} VM started (PID: $(cat "${pid_file}")). Logs: ${log_file}"
}

stop_vm() {
  local role="$1"
  local pid_file="${RUN_DIR}/${role}.pid"

  if [ -f "${pid_file}" ]; then
    local pid
    pid="$(cat "${pid_file}")"
    if kill -0 "${pid}" 2>/dev/null; then
      echo "Stopping ${role} VM (PID: ${pid})..."
      kill "${pid}" 2>/dev/null || true
      for i in $(seq 1 10); do
        kill -0 "${pid}" 2>/dev/null || break
        sleep 0.5
      done
      kill -9 "${pid}" 2>/dev/null || true
    fi
    rm -f "${pid_file}"
    echo "${role} VM stopped."
  else
    echo "${role} VM is not running."
  fi
}

reset_vm() {
  local role="$1"
  local overlay="${RUN_DIR}/${role}_overlay.qcow2"

  echo "Performing instant factory reset for ${role} VM..."
  stop_vm "${role}"
  rm -f "${overlay}"
  if [ -f "${BASE_IMG}" ]; then
    qemu-img create -f qcow2 -b "${BASE_IMG}" -F qcow2 "${overlay}" >/dev/null
    echo "Reset complete: new pristine overlay generated."
  fi
  start_vm "${role}"
}

status_lab() {
  echo "=== Test Lab Status ==="
  echo "Architecture: ${ARCH}"
  echo "Flavor:       ${FLAVOR} ${VERSION}"
  echo "Base Image:   $([ -f "${BASE_IMG}" ] && echo "Ready (${BASE_IMG})" || echo "Missing")"
  echo "-----------------------"
  for role in core ap; do
    local pid_file="${RUN_DIR}/${role}.pid"
    if [ -f "${pid_file}" ] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
      local p_prov="$([ "${role}" = "core" ] && echo 22220 || echo 22230)"
      local p_boot="$([ "${role}" = "core" ] && echo 22221 || echo 22231)"
      echo "${role} VM: RUNNING (PID: $(cat "${pid_file}"), SSH: prov=${p_prov}, boot=${p_boot})"
    else
      echo "${role} VM: STOPPED"
    fi
  done
  echo "======================="
}

cmd="${1:-status}"
shift || true

wait_ssh() {
  local role="$1"
  local port_prov="$([ "${role}" = "core" ] && echo 22220 || echo 22230)"
  local port_boot="$([ "${role}" = "core" ] && echo 22221 || echo 22231)"
  echo "Waiting for ${role} VM boot and SSH on localhost (ports ${port_prov} / ${port_boot})..."
  for i in $(seq 1 75); do
    for p in "${port_prov}" "${port_boot}"; do
      if ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=2 -p "${p}" root@127.0.0.1 "logread | grep -q 'init complete' || [ -f /etc/ansible_device_role ]" 2>/dev/null; then
        echo "${role} SSH is ready (boot complete on port ${p})!"
        return 0
      fi
    done
    sleep 1
  done
  echo "Error: Timeout waiting for ${role} SSH." >&2
  return 1
}

case "${cmd}" in
  up)
    check_qemu_installed
    target="${1:-all}"
    case "${target}" in
      core) start_vm "core" ;;
      ap)   start_vm "ap" ;;
      all)  start_vm "core"; start_vm "ap" ;;
      *) echo "Unknown target: ${target}. Choose core, ap, or all." >&2; exit 1 ;;
    esac
    ;;
  wait)
    target="${1:-all}"
    case "${target}" in
      core) wait_ssh "core" ;;
      ap)   wait_ssh "ap" ;;
      all)  wait_ssh "core"; wait_ssh "ap" ;;
      *) echo "Unknown target: ${target}." >&2; exit 1 ;;
    esac
    ;;
  down)
    target="${1:-all}"
    case "${target}" in
      core) stop_vm "core" ;;
      ap)   stop_vm "ap" ;;
      all)  stop_vm "core"; stop_vm "ap" ;;
      *) echo "Unknown target: ${target}. Choose core, ap, or all." >&2; exit 1 ;;
    esac
    ;;
  reset-core)
    check_qemu_installed
    reset_vm "core"
    ;;
  reset-ap)
    check_qemu_installed
    reset_vm "ap"
    ;;
  status)
    status_lab
    ;;
  *)
    cat <<EOF
Usage: $0 <command> [target]

Commands:
  up [core|ap|all]       Start virtual machines
  down [core|ap|all]     Stop virtual machines
  wait [core|ap|all]     Wait for VM boot and SSH readiness
  reset-core             Instant factory reset for Core Router VM
  reset-ap               Instant factory reset for Dumb AP VM
  status                 Display VM running states and ports
EOF
    exit 1
    ;;
esac
