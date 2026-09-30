#!/usr/bin/env bash
# Build gstcefsrc (cefsrc) and stage the CEF runtime next to the plugin.
set -euo pipefail

: "${GSTCEFSRC_REPO:?}"
: "${GSTCEFSRC_REF:?}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  build-essential \
  ca-certificates \
  cmake \
  curl \
  git \
  libasound2-dev \
  libatk-bridge2.0-dev \
  libatk1.0-dev \
  libcairo2-dev \
  libcups2-dev \
  libdrm-dev \
  libgbm-dev \
  libgtk-3-dev \
  libgstreamer-plugins-base1.0-dev \
  libgstreamer1.0-dev \
  libnspr4-dev \
  libnss3-dev \
  libpango1.0-dev \
  libx11-xcb-dev \
  libxcb-dri3-dev \
  libxcomposite-dev \
  libxdamage-dev \
  libxkbcommon-dev \
  libxrandr-dev \
  ninja-build \
  pkg-config

git init /src/gstcefsrc
cd /src/gstcefsrc
git remote add origin "${GSTCEFSRC_REPO}"
git fetch --depth 1 origin "${GSTCEFSRC_REF}"
git checkout FETCH_HEAD

cmake -G Ninja -S /src/gstcefsrc -B /src/gstcefsrc/build -DCMAKE_BUILD_TYPE=Release
ninja -C /src/gstcefsrc/build

plugin="$(find /src/gstcefsrc/build -name libgstcef.so -type f | head -1)"
test -n "${plugin}"
outdir="$(dirname "${plugin}")"
mkdir -p /opt/gstcef
cp -a "${outdir}/." /opt/gstcef/
test -f /opt/gstcef/libgstcef.so
test -f /opt/gstcef/gstcefsubprocess
test -f /opt/gstcef/libcef.so
test -d /opt/gstcef/locales
