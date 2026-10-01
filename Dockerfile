# syntax=docker/dockerfile:1

# Mixer image. MXL (libmxl + mxlsrc/mxlsink) and the HTML keyer (gstcefsrc)
# are compiled in this build. No separate SDK checkout is required.

ARG UBUNTU=ubuntu:24.04
ARG MXL_REPO=https://github.com/dmf-mxl/mxl.git
ARG MXL_REF=v1.1.0
ARG RUST_TOOLCHAIN=1.92
ARG GSTCEFSRC_REPO=https://github.com/centricular/gstcefsrc.git
ARG GSTCEFSRC_REF=b63340852fc93b0ab67b07200e1ff44f59ba6769

FROM ${UBUNTU} AS mxl-builder
ARG DEBIAN_FRONTEND=noninteractive
ARG MXL_REPO
ARG MXL_REF
ARG RUST_TOOLCHAIN
ENV MXL_REPO=${MXL_REPO} \
    MXL_REF=${MXL_REF} \
    RUST_TOOLCHAIN=${RUST_TOOLCHAIN}
COPY docker/build-mxl.sh /tmp/build-mxl.sh
RUN chmod +x /tmp/build-mxl.sh && /tmp/build-mxl.sh

FROM ${UBUNTU} AS cef-builder
ARG DEBIAN_FRONTEND=noninteractive
ARG GSTCEFSRC_REPO
ARG GSTCEFSRC_REF
ENV GSTCEFSRC_REPO=${GSTCEFSRC_REPO} \
    GSTCEFSRC_REF=${GSTCEFSRC_REF}
COPY docker/build-cef.sh /tmp/build-cef.sh
RUN chmod +x /tmp/build-cef.sh && /tmp/build-cef.sh

FROM ${UBUNTU}

ARG DEBIAN_FRONTEND=noninteractive
ARG FLOWXER_VERSION=0.1.0
LABEL org.opencontainers.image.version=$FLOWXER_VERSION
LABEL org.opencontainers.image.licenses="Apache-2.0"

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        python3-gi \
        python3-gi-cairo \
        python3-cairo \
        gir1.2-gstreamer-1.0 \
        gir1.2-gst-plugins-base-1.0 \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-libav \
        libgdk-pixbuf2.0-0 \
        fonts-dejavu-core \
        ffmpeg \
        libopus0 \
        libvpx9 \
        curl \
        ca-certificates \
        libnss3 \
        libnspr4 \
        libatk1.0-0t64 \
        libatk-bridge2.0-0t64 \
        libcups2t64 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0t64 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2t64 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxshmfence1 \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY --from=mxl-builder /opt/mxl /opt/mxl
COPY --from=cef-builder /opt/gstcef /opt/gstcef
RUN printf '/opt/mxl/lib\n/opt/gstcef\n' > /etc/ld.so.conf.d/flowxer-media.conf \
    && ldconfig \
    && test -f /opt/mxl/gst/libgstmxl.so \
    && test -f /opt/gstcef/libgstcef.so

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE VERSION /app/
COPY src /app/src
COPY configs /app/configs
COPY docker/entrypoint.sh /entrypoint.sh

RUN pip3 install --no-cache-dir --break-system-packages /app \
    && pip3 install --no-cache-dir --break-system-packages 'aiortc==1.9.0' \
    && python3 -c "import flowxer" \
    && chmod +x /entrypoint.sh \
    && mkdir -p /storage/clips /storage/stingers /storage/graphics /tmp/cef-cache

ENV LD_LIBRARY_PATH=/opt/mxl/lib:/opt/gstcef
ENV GST_PLUGIN_PATH=/opt/mxl/gst:/opt/gstcef:/usr/lib/x86_64-linux-gnu/gstreamer-1.0
ENV GST_CEF_CHROME_EXTRA_FLAGS=no-sandbox,disable-dev-shm-usage,use-gl=angle,use-angle=swiftshader
ENV GST_CEF_CACHE_LOCATION=/tmp/cef-cache
ENV FLOWXER_HOST=0.0.0.0
ENV FLOWXER_PORT=9610
ENV FLOWXER_MXL_DOMAIN=/mxl-domain
ENV FLOWXER_STORAGE_ROOT=/storage
ENV FLOWXER_OVERLAY_URL=http://127.0.0.1:9610/graphics/lower-third.html
ENV PYTHONUNBUFFERED=1

EXPOSE 9610
VOLUME ["/mxl-domain", "/storage"]
ENTRYPOINT ["/entrypoint.sh"]
