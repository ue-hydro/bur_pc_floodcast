# Context: checkout root. Integrated targets additionally require --build-context framework=<checkout>.
ARG BASE_IMAGE=ubuntu@sha256:78d4744ead1f5b19b04d0df558ab182a5300903900755e515b2ca4d10eb2044d
ARG MINIFORGE_IMAGE=condaforge/miniforge3@sha256:d39de5149d9819b186de0748a2117842fc4f7c0a841d6a3e1f99fd9919241dfe

FROM ${BASE_IMAGE} AS certificates
ARG CA_CERTIFICATES_VERSION
RUN test -n "$CA_CERTIFICATES_VERSION" && \
    apt-get update -o APT::Update::Error-Mode=any && \
    apt-get install --no-install-recommends -y "ca-certificates=$CA_CERTIFICATES_VERSION"

FROM ${BASE_IMAGE} AS hydrology-build
COPY --from=certificates /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ARG APT_SNAPSHOT
ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
RUN [[ "$APT_SNAPSHOT" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] && \
    apt-get update --snapshot "$APT_SNAPSHOT" -o APT::Update::Error-Mode=any && \
    apt-get install --snapshot "$APT_SNAPSHOT" --no-install-recommends -y \
      ca-certificates git cmake make gcc g++ gfortran perl pkg-config \
      libnetcdf-dev libnetcdff-dev netcdf-bin libhdf5-dev libopenblas-dev \
      libopenmpi-dev openmpi-bin python3 python3-netcdf4 && \
    rm -rf /var/lib/apt/lists/*
COPY scripts/build_hydrology.py /opt/floodcast/hydrology/recipe/build.py
ARG BUILD_JOBS=1
ARG SUNDIALS_REVISION
RUN python3 /opt/floodcast/hydrology/recipe/build.py sundials --revision "$SUNDIALS_REVISION" --jobs "$BUILD_JOBS"
ARG SUMMA_REVISION
RUN python3 /opt/floodcast/hydrology/recipe/build.py summa --revision "$SUMMA_REVISION" --jobs "$BUILD_JOBS"
ARG PIO_REVISION
ARG PIO_CMAKE_REVISION
RUN python3 /opt/floodcast/hydrology/recipe/build.py pio --revision "$PIO_REVISION" --helper-revision "$PIO_CMAKE_REVISION" --jobs "$BUILD_JOBS"
ARG MIZUROUTE_REVISION
RUN python3 /opt/floodcast/hydrology/recipe/build.py mizuroute --revision "$MIZUROUTE_REVISION" --jobs "$BUILD_JOBS"
ARG BASE_IMAGE
# Technical provenance only; solver tests are a separate operation.
RUN python3 /opt/floodcast/hydrology/recipe/build.py provenance --base "$BASE_IMAGE" --snapshot "$APT_SNAPSHOT" && \
    ldconfig

# All three integrated images share the same Ubuntu model runtime.
FROM ${BASE_IMAGE} AS model-runtime
COPY --from=hydrology-build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ARG APT_SNAPSHOT
ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
RUN [[ "$APT_SNAPSHOT" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] && \
    apt-get update --snapshot "$APT_SNAPSHOT" -o APT::Update::Error-Mode=any && \
    apt-get install --snapshot "$APT_SNAPSHOT" --no-install-recommends -y \
      ca-certificates python3 python3-venv python3-netcdf4 binutils \
      libnetcdf19t64 libnetcdff7 libopenblas0-pthread libopenmpi3t64 openmpi-bin \
      libhdf5-103-1t64 libgfortran5 libgomp1 && \
    rm -rf /var/lib/apt/lists/*
COPY --from=hydrology-build /opt/floodcast/hydrology/bin/ /opt/floodcast/hydrology/bin/
COPY --from=hydrology-build /opt/floodcast/hydrology/lib/ /opt/floodcast/hydrology/lib/
COPY --from=hydrology-build /opt/floodcast/hydrology/licenses/ /opt/floodcast/hydrology/licenses/
COPY --from=hydrology-build /opt/floodcast/hydrology/provenance/ /opt/floodcast/hydrology/provenance/
COPY --from=hydrology-build /opt/floodcast/hydrology/recipe/ /opt/floodcast/hydrology/recipe/
COPY --from=hydrology-build /opt/floodcast/hydrology/toolchain.json /opt/floodcast/hydrology/toolchain.json
RUN printf '%s\n' /opt/floodcast/hydrology/lib > /etc/ld.so.conf.d/floodcast-hydrology.conf && \
    ldconfig && mkdir -p /workspace /data && chown 65532:65532 /workspace /data
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 FLOODCAST_DATA_ROOT=/data
ENV PATH="/opt/floodcast/env/bin:/opt/floodcast/hydrology/bin:${PATH}"
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
