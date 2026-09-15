FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG FLOWXER_VERSION=0.1.0
LABEL org.opencontainers.image.version=$FLOWXER_VERSION

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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE VERSION /app/
COPY src /app/src
COPY configs /app/configs
COPY docker/entrypoint.sh /entrypoint.sh

RUN pip3 install --no-cache-dir --break-system-packages /app \
    && pip3 install --no-cache-dir --break-system-packages 'aiortc==1.9.0' \
    && python3 -c "import flowxer" \
    && chmod +x /entrypoint.sh \
    && mkdir -p /mxl-domain /storage/clips /storage/stingers /storage/graphics \
    && cp /app/configs/domain_def.json /mxl-domain/domain_def.json

# Optional: copy a pre-built MXL GStreamer plugin into the image.
#   docker build --build-context mxlplugins=/path/to/gst-mxl ...
# libgstmxl.so and libmxl.so can also be bind-mounted at runtime.
ENV LD_LIBRARY_PATH=/opt/mxl/lib
ENV GST_PLUGIN_PATH=/usr/lib/x86_64-linux-gnu/gstreamer-1.0:/opt/mxl/gst
ENV FLOWXER_HOST=0.0.0.0
ENV FLOWXER_PORT=9610
ENV FLOWXER_MXL_DOMAIN=/mxl-domain
ENV FLOWXER_STORAGE_ROOT=/storage
ENV FLOWXER_OVERLAY_URL=http://127.0.0.1:9610/graphics/lower-third.html
ENV PYTHONUNBUFFERED=1

EXPOSE 9610
VOLUME ["/mxl-domain", "/storage"]
ENTRYPOINT ["/entrypoint.sh"]
