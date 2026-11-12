# Context: checkout root. Integrated targets additionally require --build-context framework=<checkout>.
ARG BASE_IMAGE=ubuntu@sha256:78d4744ead1f5b19b04d0df558ab182a5300903900755e515b2ca4d10eb2044d
ARG MINIFORGE_IMAGE=condaforge/miniforge3@sha256:d39de5149d9819b186de0748a2117842fc4f7c0a841d6a3e1f99fd9919241dfe

FROM ${BASE_IMAGE} AS hydraulics-build
ARG BASE_IMAGE
ARG APT_SNAPSHOT
ARG BOOTSTRAP_CA_VERSION
ARG FLUXOS_REPOSITORY
ARG FLUXOS_REVISION
ARG BUILD_JOBS=1
ENV DEBIAN_FRONTEND=noninteractive
RUN test -n "$APT_SNAPSHOT" && test -n "$BOOTSTRAP_CA_VERSION" && \
    apt-get update -o APT::Update::Error-Mode=any && \
    mkdir -p /tmp/floodcast-ca /opt/floodcast/share/hydraulics && \
    cd /tmp/floodcast-ca && \
    apt-get download "ca-certificates=$BOOTSTRAP_CA_VERSION" && \
    sha256sum ca-certificates*.deb > /opt/floodcast/share/hydraulics/bootstrap-ca.sha256 && \
    dpkg-deb -x ca-certificates*.deb extracted && \
    cat extracted/usr/share/ca-certificates/mozilla/*.crt > roots.pem && \
    apt-get update --snapshot "$APT_SNAPSHOT" -o APT::Update::Error-Mode=any \
      -o Acquire::https::CaInfo=/tmp/floodcast-ca/roots.pem && \
    apt-get install --snapshot "$APT_SNAPSHOT" -y --no-install-recommends \
      -o Acquire::https::CaInfo=/tmp/floodcast-ca/roots.pem \
      build-essential cmake git ca-certificates libarmadillo-dev libhdf5-dev python3 binutils && \
    cd / && rm -rf /var/lib/apt/lists/* /tmp/floodcast-ca
COPY scripts/build_fluxos.sh scripts/toolchain.py /opt/floodcast/recipe/
COPY scripts/patch_fluxos.py /opt/floodcast/recipe/patch_source.py
RUN BASE_IMAGE="$BASE_IMAGE" APT_SNAPSHOT="$APT_SNAPSHOT" \
    FLUXOS_REPOSITORY="$FLUXOS_REPOSITORY" FLUXOS_REVISION="$FLUXOS_REVISION" \
    BUILD_JOBS="$BUILD_JOBS" bash /opt/floodcast/recipe/build_fluxos.sh

# Only libraries linked by this FLUXOS build are needed in the integrated runtime.
FROM ${BASE_IMAGE} AS model-runtime
COPY --from=hydraulics-build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ARG APT_SNAPSHOT
ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
RUN [[ "$APT_SNAPSHOT" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] && \
    apt-get update --snapshot "$APT_SNAPSHOT" -o APT::Update::Error-Mode=any && \
    apt-get install --snapshot "$APT_SNAPSHOT" --no-install-recommends -y \
      ca-certificates python3 python3-venv libgomp1 libstdc++6 && \
    rm -rf /var/lib/apt/lists/*
COPY --from=hydraulics-build /opt/floodcast/bin/ /opt/floodcast/bin/
COPY --from=hydraulics-build /opt/floodcast/recipe/ /opt/floodcast/recipe/
COPY --from=hydraulics-build /opt/floodcast/share/hydraulics/ /opt/floodcast/share/hydraulics/
# Keep build provenance intact and record the distinct installed runtime packages.
RUN /usr/bin/python3 - <<'PY'
import hashlib
import json
import subprocess
from pathlib import Path

share = Path('/opt/floodcast/share/hydraulics')
packages = subprocess.run(
    ['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\n'],
    check=True, text=True, capture_output=True, timeout=30,
).stdout.strip()
runtime = {
    'schema_version': 1,
    'model_toolchain_sha256': hashlib.sha256((share / 'toolchain.json').read_bytes()).hexdigest(),
    'packages': packages,
}
(share / 'runtime.json').write_text(json.dumps(runtime, indent=2) + '\n', encoding='utf-8')
PY
RUN mkdir -p /workspace /data && chown 65532:65532 /workspace /data
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 FLOODCAST_DATA_ROOT=/data
ENV PATH="/opt/floodcast/env/bin:/opt/floodcast/bin:${PATH}"
WORKDIR /workspace

# pip builds package metadata and checks Requires-Python using its normal resolver.
FROM model-runtime AS wheel-builder
COPY --from=framework /pyproject.toml /build/project/pyproject.toml
COPY --from=framework /src/floodcast/ /build/project/src/floodcast/
RUN python3 -m venv /tmp/build-env && \
    /tmp/build-env/bin/python -m pip wheel --wheel-dir /wheels /build/project
FROM ${MINIFORGE_IMAGE} AS conda-builder
COPY --from=framework /environment.yml /build/environment.yml
RUN conda env create --prefix /opt/floodcast/env --file /build/environment.yml
COPY --from=wheel-builder /wheels/ /wheels/
RUN /opt/floodcast/env/bin/python -m pip install --no-index --find-links=/wheels /wheels/floodcast-*.whl && \
    rm -rf /wheels

FROM model-runtime AS framework-runtime
RUN mkdir -p /work /output /results && chown 65532:65532 /work /output /results
ENV FLOODCAST_RUNTIME=local FLOODCAST_DATA_ROOT=/results

FROM framework-runtime AS pip
COPY --from=wheel-builder /wheels/ /wheels/
RUN python3 -m venv /opt/floodcast/env && \
    /opt/floodcast/env/bin/python -m pip install --no-index --find-links=/wheels /wheels/floodcast-*.whl && \
    rm -rf /wheels
LABEL org.floodcast.installation-method="pip"
USER 65532:65532
ENTRYPOINT ["python", "-m", "floodcast"]
CMD ["--help"]

FROM framework-runtime AS conda
# Keep the environment at its original absolute prefix; no Conda manager is shipped.
COPY --from=conda-builder /opt/floodcast/env/ /opt/floodcast/env/
LABEL org.floodcast.installation-method="conda"
USER 65532:65532
ENTRYPOINT ["python", "-m", "floodcast"]
CMD ["--help"]

FROM framework-runtime AS source-manual
COPY --from=framework /pyproject.toml /opt/floodcast/source/pyproject.toml
COPY --from=framework /src/floodcast/ /opt/floodcast/source/src/floodcast/
RUN python3 -m venv /opt/floodcast/env && \
    /opt/floodcast/env/bin/python -m pip install --editable /opt/floodcast/source
LABEL org.floodcast.installation-method="source-manual"
USER 65532:65532
ENTRYPOINT ["python", "-m", "floodcast"]
CMD ["--help"]

# Model-only target for Floodcast running on the host; no compiler toolchain.
FROM model-runtime AS standalone
WORKDIR /work
USER 65532:65532
CMD ["/bin/bash"]
