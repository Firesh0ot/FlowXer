# syntax=docker/dockerfile:1
# flowxer-engine (mix engine prototype P1). Build context: engine/.
# Pins as in mxl-multiviewer / mxl-replay: CUDA 12.8.2 on Ubuntu 24.04, MXL 218ddaa.
# FFmpeg is Ubuntu's (h264_nvenc loads libnvidia-encode at run time). libcuda, NVENC and
# NVML come from the NVIDIA container toolkit when the container starts with a GPU
# (NVIDIA_DRIVER_CAPABILITIES below); libcudart is linked statically.

# Must precede the first FROM.
ARG CUDA_IMAGE=nvidia/cuda:12.8.2-devel-ubuntu24.04
# The engine starts this MediaMTX itself when no shared one is given (PREVIEW_PUBLISH_URL unset).
ARG MEDIAMTX_IMAGE=bluenviron/mediamtx:1.20.1

FROM ${MEDIAMTX_IMAGE} AS mediamtx

FROM ${CUDA_IMAGE} AS deps
ARG MXL_REF=218ddaa0a08c12ffe75fc475ae65aa3d9eef16d7
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build git pkg-config curl zip unzip tar ca-certificates \
        libavcodec-dev libavformat-dev libavutil-dev nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/microsoft/vcpkg /opt/vcpkg \
    && /opt/vcpkg/bootstrap-vcpkg.sh -disableMetrics
ENV VCPKG_ROOT=/opt/vcpkg
RUN git init /tmp/mxl \
    && git -C /tmp/mxl remote add origin https://github.com/dmf-mxl/mxl.git \
    && git -C /tmp/mxl fetch --depth 1 origin "${MXL_REF}" \
    && git -C /tmp/mxl checkout FETCH_HEAD
COPY docker/mxl-vcpkg.json /tmp/mxl-manifest/vcpkg.json
RUN BASELINE="$(git -C /opt/vcpkg rev-parse HEAD)" \
    && sed -i "1a\\  \"builtin-baseline\": \"${BASELINE}\"," /tmp/mxl-manifest/vcpkg.json
RUN cmake -S /tmp/mxl -B /tmp/mxl/build -G Ninja \
        -DCMAKE_TOOLCHAIN_FILE=/opt/vcpkg/scripts/buildsystems/vcpkg.cmake \
        -DVCPKG_MANIFEST_DIR=/tmp/mxl-manifest \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_TESTS=OFF -DBUILD_TOOLS=OFF -DBUILD_UTILS=OFF -DBUILD_DOCS=OFF \
        -DMXL_ENABLE_FABRICS_OFI=OFF \
        -DCMAKE_INSTALL_PREFIX=/opt/mxl \
    && cmake --build /tmp/mxl/build -j"$(nproc)" \
    && cmake --install /tmp/mxl/build

FROM deps AS build
COPY . /src
# The unit tests need no GPU; the kernels are checked on a GPU host with --selftest.
RUN cmake -S /src -B /src/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_PREFIX_PATH="/opt/mxl;/tmp/mxl/build/vcpkg_installed/x64-linux" \
    && cmake --build /src/build -j"$(nproc)" \
    && /src/build/fxeng-tests

FROM ubuntu:24.04 AS runtime
ARG MXL_REF=218ddaa0a08c12ffe75fc475ae65aa3d9eef16d7
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates libavcodec60 libavformat60 libavutil58 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/mxl/lib/ /usr/local/lib/
COPY --from=build /src/build/flowxer-engine /usr/local/bin/flowxer-engine
COPY --from=mediamtx /mediamtx /usr/local/bin/mediamtx
RUN ldconfig
# ubuntu:24.04 has uid/gid 1000 ("ubuntu"); the MXL domains on the hosts belong to 1000.
USER 1000:1000
ENV NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
LABEL org.opencontainers.image.source="https://github.com/Firesh0ot/FlowXer" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.title="flowxer-engine" \
      io.dmf.mxl.revision="${MXL_REF}"
# 9630 API/metrics/test page; own MediaMTX: 8989 WHEP, 8988 HLS, 8289/udp+tcp ICE.
EXPOSE 9630 8989 8988 8289 8289/udp
ENTRYPOINT ["/usr/local/bin/flowxer-engine"]
