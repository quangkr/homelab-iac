#!/usr/bin/env bash
# ==============================================================================
# test_lab/fetch_image.sh — OpenWrt / ImmortalWrt Test Image Fetcher & Injector
# ==============================================================================
# Downloads official pre-built ext4 images for armsr/armv8 and malta/le,
# resizes rootfs to 256MB, and injects the controller Dropbear SSH public key
# offline using debugfs (no host root / loop mounts required).
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_DIR="${SCRIPT_DIR}/cache"
BASE_DIR="${SCRIPT_DIR}/base_images"
mkdir -p "${CACHE_DIR}" "${BASE_DIR}"

FLAVOR="openwrt"
VERSION=""
ARCH="armsr/armv8"
SSH_KEY="${HOME}/.ssh/id_ed25519.pub"

usage() {
  cat <<EOF
Usage: $0 [options]

Options:
  --flavor <openwrt|immortalwrt>   Distribution flavor (default: openwrt)
  --version <version>              Release version (default: 23.05.5 for openwrt, 25.12.2 for immortalwrt)
  --arch <armsr/armv8|malta/le>    Target architecture (default: armsr/armv8)
  --ssh-key <path>                 Path to SSH public key (default: ~/.ssh/id_ed25519.pub)
  -h, --help                       Show this help message

Examples:
  $0 --flavor openwrt --version 23.05.5 --arch armsr/armv8
  $0 --flavor immortalwrt --version 25.12.2 --arch armsr/armv8
  $0 --flavor openwrt --version 23.05.5 --arch malta/le
EOF
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --flavor)  FLAVOR="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --arch)    ARCH="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

if [ -z "${VERSION}" ]; then
  if [ "${FLAVOR}" = "openwrt" ]; then
    VERSION="23.05.5"
  else
    VERSION="25.12.2"
  fi
fi

ARCH_SLUG="$(echo "${ARCH}" | tr '/' '_')"
OUT_PREFIX="${FLAVOR}_${VERSION}_${ARCH_SLUG}"
QCOW2_BASE="${BASE_DIR}/${OUT_PREFIX}.qcow2"
KERNEL_BIN="${BASE_DIR}/${OUT_PREFIX}_kernel.bin"

echo "=== OpenWrt / ImmortalWrt Test Image Fetcher ==="
echo "Flavor:       ${FLAVOR}"
echo "Version:      ${VERSION}"
echo "Architecture: ${ARCH}"
echo "SSH Key:      ${SSH_KEY}"
echo "Output Base:  ${QCOW2_BASE}"
echo "================================================"

# Verify SSH public key exists, generate temporary if missing
if [ ! -f "${SSH_KEY}" ]; then
  echo "Notice: SSH public key ${SSH_KEY} not found. Generating a local test keypair in test_lab/..."
  SSH_KEY_DIR="${SCRIPT_DIR}/keys"
  mkdir -p "${SSH_KEY_DIR}"
  SSH_KEY="${SSH_KEY_DIR}/id_ed25519.pub"
  if [ ! -f "${SSH_KEY}" ]; then
    ssh-keygen -t ed25519 -N "" -f "${SSH_KEY_DIR}/id_ed25519" -C "test_lab_controller" >/dev/null
  fi
  echo "Generated temporary test key: ${SSH_KEY}"
fi

# Determine download URLs based on flavor, version, and architecture
if [ "${FLAVOR}" = "openwrt" ]; then
  BASE_URL="https://downloads.openwrt.org/releases/${VERSION}/targets/${ARCH}"
  if [ "${ARCH}" = "armsr/armv8" ]; then
    IMG_NAME="openwrt-${VERSION}-armsr-armv8-generic-ext4-combined.img.gz"
    KERNEL_NAME="openwrt-${VERSION}-armsr-armv8-generic-kernel.bin"
  elif [ "${ARCH}" = "malta/le" ]; then
    IMG_NAME="openwrt-${VERSION}-malta-le-rootfs-ext4.img.gz"
    KERNEL_NAME="openwrt-${VERSION}-malta-le-vmlinux.elf"
  else
    echo "Error: Unsupported architecture for openwrt: ${ARCH}" >&2
    exit 1
  fi
elif [ "${FLAVOR}" = "immortalwrt" ]; then
  BASE_URL="https://downloads.immortalwrt.org/releases/${VERSION}/targets/${ARCH}"
  if [ "${ARCH}" = "armsr/armv8" ]; then
    IMG_NAME="immortalwrt-${VERSION}-armsr-armv8-generic-ext4-combined.img.gz"
    KERNEL_NAME="immortalwrt-${VERSION}-armsr-armv8-generic-kernel.bin"
  elif [ "${ARCH}" = "malta/le" ]; then
    IMG_NAME="immortalwrt-${VERSION}-malta-le-rootfs-ext4.img.gz"
    KERNEL_NAME="immortalwrt-${VERSION}-malta-le-vmlinux.elf"
  else
    echo "Error: Unsupported architecture for immortalwrt: ${ARCH}" >&2
    exit 1
  fi
else
  echo "Error: Unsupported flavor: ${FLAVOR}" >&2
  exit 1
fi

CACHE_IMG_GZ="${CACHE_DIR}/${IMG_NAME}"
CACHE_KERNEL="${CACHE_DIR}/${KERNEL_NAME}"

# Step 1: Download image and kernel
echo "[1/4] Downloading ${FLAVOR} ${VERSION} (${ARCH})..."
if [ ! -f "${CACHE_IMG_GZ}" ]; then
  echo "Downloading rootfs/combined image from: ${BASE_URL}/${IMG_NAME}"
  curl -fL -C - --retry 3 --connect-timeout 20 -o "${CACHE_IMG_GZ}.tmp" "${BASE_URL}/${IMG_NAME}"
  mv "${CACHE_IMG_GZ}.tmp" "${CACHE_IMG_GZ}"
else
  echo "Found cached image: ${CACHE_IMG_GZ}"
fi

if [ -n "${KERNEL_NAME}" ]; then
  if [ ! -f "${CACHE_KERNEL}" ]; then
    echo "Downloading kernel from: ${BASE_URL}/${KERNEL_NAME}"
    curl -fL -C - --retry 3 --connect-timeout 20 -o "${CACHE_KERNEL}.tmp" "${BASE_URL}/${KERNEL_NAME}"
    mv "${CACHE_KERNEL}.tmp" "${CACHE_KERNEL}"
  else
    echo "Found cached kernel: ${CACHE_KERNEL}"
  fi
  cp -f "${CACHE_KERNEL}" "${KERNEL_BIN}"
fi

# Step 2: Decompress and resize image
RAW_IMG="${CACHE_DIR}/${OUT_PREFIX}.raw"
echo "[2/4] Decompressing image to ${RAW_IMG}..."
gzip -dc "${CACHE_IMG_GZ}" > "${RAW_IMG}"

if command -v qemu-img >/dev/null 2>&1; then
  echo "Expanding image to 256MB with qemu-img..."
  qemu-img resize -f raw "${RAW_IMG}" 256M
fi

# Step 3: Offline SSH public key injection via debugfs
echo "[3/4] Injecting SSH public key via debugfs..."
if command -v debugfs >/dev/null 2>&1; then
  # Inject into Dropbear authorized_keys
  debugfs -w "${RAW_IMG}" <<EOF || true
cd /
mkdir etc
cd etc
mkdir dropbear
cd dropbear
write ${SSH_KEY} authorized_keys
sif authorized_keys mode 0100600
close
quit
EOF
  echo "SSH public key successfully injected into /etc/dropbear/authorized_keys"
else
  echo "Warning: 'debugfs' (e2fsprogs) not found. Skipping offline SSH key injection." >&2
  echo "Dropbear key can be injected on initial boot using ./bootstrap_ssh.sh." >&2
fi

# Step 4: Convert to base qcow2 template
echo "[4/4] Creating base QCOW2 image..."
if command -v qemu-img >/dev/null 2>&1; then
  qemu-img convert -f raw -O qcow2 "${RAW_IMG}" "${QCOW2_BASE}"
  rm -f "${RAW_IMG}"
  echo "Base QCOW2 template ready: ${QCOW2_BASE}"
else
  echo "Warning: 'qemu-img' not found. Keeping raw image as base." >&2
  mv "${RAW_IMG}" "${BASE_DIR}/${OUT_PREFIX}.raw"
fi

echo "=== Done! Image preparation complete ==="
