# syntax=docker/dockerfile:1

# Mixer image: FlowXer's control plane (Python: API, state, NMOS, tally, library) and its media
# engine (C++/CUDA, engine/), which the control plane starts as a child process. The engine
# needs an NVIDIA GPU at run time (NVIDIA_DRIVER_CAPABILITIES below).
#
# Pins as in mxl-multiviewer / mxl-replay: CUDA 12.8.2 on Ubuntu 24.04, MXL 218ddaa. FFmpeg is
# Ubuntu's (NVENC for the preview mosaic, NVDEC for clips: loaded from the driver at run time).
# libcudart is linked statically; libcuda, NVENC, NVDEC and NVML come from the NVIDIA container
# toolkit when the container starts with a GPU.

# Must precede the first FROM.
ARG UBUNTU=ubuntu:24.04
ARG CUDA_IMAGE=nvidia/cuda:12.8.2-devel-ubuntu24.04
# The engine starts this MediaMTX itself when no shared one is given (PREVIEW_PUBLISH_URL unset).
ARG MEDIAMTX_IMAGE=bluenviron/mediamtx:1.20.1
ARG MXL_REF=218ddaa0a08c12ffe75fc475ae65aa3d9eef16d7

FROM ${MEDIAMTX_IMAGE} AS mediamtx

FROM ${CUDA_IMAGE} AS engine-deps
ARG MXL_REF
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
COPY engine/docker/mxl-vcpkg.json /tmp/mxl-manifest/vcpkg.json
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

FROM engine-deps AS engine-build
COPY engine /src
# The unit tests need no GPU; the kernels are checked on a GPU host with --selftest.
RUN cmake -S /src -B /src/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_PREFIX_PATH="/opt/mxl;/tmp/mxl/build/vcpkg_installed/x64-linux" \
    && cmake --build /src/build -j"$(nproc)" \
    && /src/build/fxeng-tests

FROM ${UBUNTU}

ARG DEBIAN_FRONTEND=noninteractive
ARG FLOWXER_VERSION=0.1.0
ARG MXL_REF
ARG VCS_REF=unknown
LABEL org.opencontainers.image.version=$FLOWXER_VERSION
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.source="https://github.com/Firesh0ot/FlowXer"
LABEL org.opencontainers.image.revision=$VCS_REF
LABEL io.dmf.mxl.revision=$MXL_REF

# ffmpeg: the library's conversions (and the libav* the engine links); fonts for generated
# stinger and still pictures.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        ffmpeg \
        libavcodec60 \
        libavformat60 \
        libavutil58 \
        fonts-dejavu-core \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=engine-build /opt/mxl/lib/ /usr/local/lib/
COPY --from=engine-build /src/build/flowxer-engine /usr/local/bin/flowxer-engine
COPY --from=mediamtx /mediamtx /usr/local/bin/mediamtx
RUN ldconfig && flowxer-engine --version

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE VERSION /app/
COPY src /app/src
COPY configs /app/configs
COPY docker/entrypoint.sh /entrypoint.sh

RUN pip3 install --no-cache-dir --break-system-packages /app \
    && python3 -c "import flowxer" \
    && chmod +x /entrypoint.sh \
    && mkdir -p /storage/clips /storage/stingers /storage/library /tmp/flowxer-engine \
    && mkdir -p /opt/mxl && printf '%s\n' "$MXL_REF" > /opt/mxl/REF \
    && chown -R 1000:1000 /storage /tmp/flowxer-engine

ENV NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
ENV HOME=/tmp
ENV FLOWXER_HOST=127.0.0.1
ENV FLOWXER_PORT=9610
ENV FLOWXER_MXL_ROOT=/Volumes/mxl
ENV FLOWXER_STORAGE_ROOT=/storage
ENV FLOWXER_MXL_REVISION=$MXL_REF
ENV PYTHONUNBUFFERED=1

# 9610 API, 3252/3253 NMOS; the engine's own MediaMTX (without PREVIEW_PUBLISH_URL): 8989 WHEP,
# 8988 HLS, 8289/udp+tcp ICE. The engine's control port (9630) is on localhost only.
EXPOSE 9610 3252 3253 8989 8988 8289 8289/udp
VOLUME ["/Volumes/mxl", "/storage", "/config"]
USER 1000:1000
ENTRYPOINT ["/entrypoint.sh"]
