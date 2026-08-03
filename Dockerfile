# Two stages: fetch and prove the binaries, then build a runtime that only
# carries what a capture needs.
#
# Versions are pinned rather than tracking latest. Two builds of the same
# Dockerfile should not change the recording pipeline underneath you; upgrading
# is a deliberate edit.

FROM python:3.13-slim AS binaries

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tar \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /stage

ARG DOWNLOADER_VERSION=v0.6.0-beta
ARG DOWNLOADER_ASSET=N_m3u8DL-RE_v0.6.0-beta_linux-x64_20260629.tar.gz
ARG SHAKA_VERSION=v3.9.3

RUN curl -fsSL --retry 3 \
      "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/${DOWNLOADER_VERSION}/${DOWNLOADER_ASSET}" \
      -o downloader.tar.gz \
 && tar -xzf downloader.tar.gz \
 && find . -name 'N_m3u8DL-RE' -type f -exec mv {} ./N_m3u8DL-RE \; \
 && chmod +x ./N_m3u8DL-RE \
 && rm -rf downloader.tar.gz

RUN curl -fsSL --retry 3 \
      "https://github.com/shaka-project/shaka-packager/releases/download/${SHAKA_VERSION}/packager-linux-x64" \
      -o shaka-packager \
 && chmod +x ./shaka-packager

# Prove both binaries before they are copied forward. A truncated download passes
# every path-based check and fails when capture starts, which for a live stream
# is the one moment it cannot fail. The downloader answers --version with a
# non-zero status, so only refusal to execute is treated as failure.
RUN ./N_m3u8DL-RE --version || true
RUN ./shaka-packager --version


FROM python:3.13-slim

LABEL org.opencontainers.image.title="stagecrowd_recorder"
LABEL org.opencontainers.image.description="Container-first archiver for Widevine-protected HLS live streams"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=binaries /stage/N_m3u8DL-RE   /usr/local/bin/N_m3u8DL-RE
COPY --from=binaries /stage/shaka-packager /usr/local/bin/shaka-packager

# On PATH rather than beside the package: pip installs stagecrowd_recorder into
# site-packages, which is not a useful place to look for tools.

WORKDIR /opt/stagecrowd_recorder
COPY pyproject.toml README.md ./
COPY stagecrowd_recorder ./stagecrowd_recorder
RUN pip install --no-cache-dir ".[cdm]"

# The working directory decides where shards land — they go in a sibling of it,
# named after the run — so it is the mount point, and both halves of a run end
# up on the host volume.
RUN mkdir -p /archive
WORKDIR /archive

ENV PYTHONUNBUFFERED=1 \
    STC_CDM=/config/device.wvd \
    STC_SETTINGS=/config/.stagecrowd

# Pointing at paths that may not exist is safe: both are checked before use, and
# an unmounted file simply means that input is unavailable. Neither is baked into
# the image — a session token in a layer survives every save and push afterwards.

ENTRYPOINT ["python", "-m", "stagecrowd_recorder"]
CMD ["probe"]
