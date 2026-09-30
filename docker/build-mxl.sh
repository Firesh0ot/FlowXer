#!/usr/bin/env bash
# Build the MXL SDK and the gst-mxl-rs plugin (mxlsrc / mxlsink) from source.
set -euo pipefail

: "${MXL_REPO:?}"
: "${MXL_REF:?}"
: "${RUST_TOOLCHAIN:?}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  bison \
  build-essential \
  ca-certificates \
  clang-19 \
  cmake \
  curl \
  flex \
  git \
  libclang-19-dev \
  libgstreamer-plugins-base1.0-dev \
  libgstreamer1.0-dev \
  lld-19 \
  llvm-19 \
  ninja-build \
  pkg-config \
  python3 \
  python3-pip \
  tar \
  unzip \
  zip

update-alternatives --install /usr/bin/clang clang /usr/bin/clang-19 100
update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-19 100
if [[ -x /usr/bin/lld-19 ]]; then
  update-alternatives --install /usr/bin/lld lld /usr/bin/lld-19 100
fi
if [[ -x /usr/bin/ld.lld-19 ]]; then
  update-alternatives --install /usr/bin/ld.lld ld.lld /usr/bin/ld.lld-19 100
fi

# Linux-Clang-Release sets CMAKE_LINKER_TYPE, which needs CMake >= 3.29.
pip3 install --break-system-packages 'cmake>=3.30'
hash -r

curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs \
  | sh -s -- -y --default-toolchain "${RUST_TOOLCHAIN}" --profile minimal
# shellcheck disable=SC1091
source /root/.cargo/env
export LIBCLANG_PATH=/usr/lib/llvm-19/lib

# Full history: MXL's vcpkg baseline is an older commit, and a depth-1 clone cannot check it out.
git clone --filter=blob:none https://github.com/microsoft/vcpkg.git /root/vcpkg
/root/vcpkg/bootstrap-vcpkg.sh -disableMetrics
command -v ninja
command -v cmake

git init /mxl
cd /mxl
git remote add origin "${MXL_REPO}"
git fetch --depth 1 origin "${MXL_REF}"
git checkout FETCH_HEAD

cd /mxl/rust
cargo build --manifest-path Cargo.toml -p gst-mxl-rs --release --locked

mxl_out="$(find /mxl/rust/target/release/build -type d -path '*/mxl-sys-*/out' | head -1)"
test -n "${mxl_out}"
test -d "${mxl_out}/lib"
mkdir -p /opt/mxl/lib /opt/mxl/gst
cp -a "${mxl_out}/lib/." /opt/mxl/lib/
if [[ -d "${mxl_out}/build/lib/internal" ]]; then
  cp -a "${mxl_out}/build/lib/internal/." /opt/mxl/lib/
fi
install -m 0644 /mxl/rust/target/release/libgstmxl.so /opt/mxl/gst/libgstmxl.so

mapfile -t libs < <(find /opt/mxl -type f \( -name '*.so' -o -name '*.so.*' \))
if [[ ${#libs[@]} -gt 0 ]]; then
  ldd "${libs[@]}" | awk '/=> \// {print $3}' | sort -u | while read -r lib; do
    case "${lib}" in
      /opt/mxl/*|/lib/*|/lib64/*|/usr/lib/*) ;;
      *) cp -a "${lib}" /opt/mxl/lib/ ;;
    esac
  done
fi

test -f /opt/mxl/gst/libgstmxl.so
find /opt/mxl/lib -name 'libmxl.so*' | grep -q .
