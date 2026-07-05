# syntax=docker/dockerfile:1
# ====================================================================
# Stage 1: Build frontend dist (only the build output is carried over)
# ====================================================================
FROM node:20-slim AS frontend-builder
WORKDIR /app
COPY frontend/package*.json ./frontend/
RUN cd frontend && npm ci
COPY frontend ./frontend
RUN cd frontend && npm run build

# ====================================================================
# Stage 2: Build Python venvs + TeX Live (heavy; build tools live here only)
# ====================================================================
FROM python:3.12-slim-bookworm AS backend-builder

ARG TEXLIVE_YEAR=2025
ARG TEXLIVE_REPOSITORY=
ARG TORCH_VARIANT=cpu

# Build-only deps. These never reach the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
        perl \
        xz-utils \
        fontconfig \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# SiGMA venv.
# torch install is selected by TORCH_VARIANT:
#   cpu (default) — pulls torch from the CPU-only index (~190MB, no CUDA/triton)
#   gpu           — pulls torch from default PyPI (~890MB + ~3.4GB of nvidia-*
#                   CUDA libs + triton); requires NVIDIA Container Toolkit on
#                   the host and `docker run --gpus all`.
# SiGMA's small embedding / rerank models run fine on CPU; only enable gpu if
# you have an NVIDIA GPU and want faster inference.
COPY backend/requirements.txt /tmp/requirements-sigma.txt
RUN python -m venv /opt/venv-sigma \
    && /opt/venv-sigma/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
    # torch and torchvision must come from the same index — mixing the CPU
    # torch wheel with a CUDA-built torchvision wheel (pulled transitively
    # from default PyPI) breaks the C++ operator ABI at import time
    # ("operator torchvision::nms does not exist").
    && if [ "${TORCH_VARIANT}" = "gpu" ]; then \
        /opt/venv-sigma/bin/pip install --no-cache-dir \
            torch==2.11.0 torchvision==0.26.0; \
       else \
        /opt/venv-sigma/bin/pip install --no-cache-dir \
            --index-url https://download.pytorch.org/whl/cpu \
            --extra-index-url https://pypi.org/simple \
            torch==2.11.0 torchvision==0.26.0; \
       fi \
    && /opt/venv-sigma/bin/pip install --no-cache-dir \
        -r /tmp/requirements-sigma.txt

# Dedicated Jupyter venv (kept separate so user pip installs in notebooks
# can never contaminate the SiGMA venv).
COPY backend/requirements-jupyter.txt /tmp/requirements-jupyter.txt
RUN python -m venv /opt/venv-jupyter \
    && /opt/venv-jupyter/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /opt/venv-jupyter/bin/pip install --no-cache-dir \
        -r /tmp/requirements-jupyter.txt

# TeX Live scheme-basic. The frontend LaTeX manager panel can upgrade to
# scheme-full / install individual packages via tlmgr at runtime; those
# installs persist on the sigma_texlive named volume.
RUN mkdir -p /tmp/install-tl \
    && TEXLIVE_REPO="${TEXLIVE_REPOSITORY:-https://ftp.math.utah.edu/pub/tex/historic/systems/texlive/${TEXLIVE_YEAR}/tlnet-final}" \
    && wget -qO- "${TEXLIVE_REPO}/install-tl-unx.tar.gz" \
        | tar -xz -C /tmp/install-tl --strip-components=1 \
    && printf '%s\n' \
        "selected_scheme scheme-basic" \
        "TEXDIR /usr/local/texlive/${TEXLIVE_YEAR}" \
        "TEXMFCONFIG ~/.texlive${TEXLIVE_YEAR}/texmf-config" \
        "TEXMFHOME ~/texmf" \
        "TEXMFLOCAL /usr/local/texlive/texmf-local" \
        "TEXMFSYSCONFIG /usr/local/texlive/${TEXLIVE_YEAR}/texmf-config" \
        "TEXMFSYSVAR /usr/local/texlive/${TEXLIVE_YEAR}/texmf-var" \
        "TEXMFVAR ~/.texlive${TEXLIVE_YEAR}/texmf-var" \
        "binary_x86_64-linux 1" \
        "instopt_adjustpath 0" \
        "instopt_adjustrepo 1" \
        "instopt_letter 0" \
        "instopt_portable 0" \
        "tlpdbopt_autobackup 0" \
        "tlpdbopt_create_formats 1" \
        "tlpdbopt_desktop_integration 0" \
        "tlpdbopt_file_assocs 0" \
        "tlpdbopt_generate_updmap 1" \
        "tlpdbopt_install_docfiles 0" \
        "tlpdbopt_install_srcfiles 0" \
        > /tmp/texlive.profile \
    && /tmp/install-tl/install-tl -repository "${TEXLIVE_REPO}" -profile /tmp/texlive.profile \
    && /usr/local/texlive/${TEXLIVE_YEAR}/bin/x86_64-linux/tlmgr option repository "${TEXLIVE_REPO}" \
    && /usr/local/texlive/${TEXLIVE_YEAR}/bin/x86_64-linux/tlmgr install --repository "${TEXLIVE_REPO}" \
        latexmk \
        texcount \
        synctex \
        etoolbox \
        xetex \
    && wget -qO /usr/local/texlive/${TEXLIVE_YEAR}/update-tlmgr-latest.sh \
        https://mirror.ctan.org/systems/texlive/tlnet/update-tlmgr-latest.sh \
    && chmod +x /usr/local/texlive/${TEXLIVE_YEAR}/update-tlmgr-latest.sh \
    && ln -sfn "${TEXLIVE_YEAR}" /usr/local/texlive/current \
    && rm -rf /tmp/install-tl /tmp/texlive.profile

# ====================================================================
# Stage 3: Final runtime image (small, no build tools)
# ====================================================================
FROM python:3.12-slim-bookworm

ARG TEXLIVE_YEAR=2025
ENV DEBIAN_FRONTEND=noninteractive \
    SIGMA_USERDATA_DIR=/app/userdata \
    SIGMA_JUPYTER_BIN=/opt/venv-jupyter/bin/jupyter \
    SIGMA_TEXLIVE_YEAR=${TEXLIVE_YEAR} \
    SIGMA_TEXLIVE_ROOT=/usr/local/texlive \
    SIGMA_TLMGR_BIN=/usr/local/texlive/current/bin/x86_64-linux/tlmgr \
    SIGMA_UPDATE_TLMGR_BIN=/usr/local/texlive/current/update-tlmgr-latest.sh \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/opt/venv-sigma/bin:/usr/local/texlive/current/bin/x86_64-linux:$PATH

WORKDIR /app

# Runtime apt deps only. No make/nodejs/npm/build-essential — those were
# only needed at build time. git IS needed at runtime: git_service runs
# `git init` / `git commit` for project snapshots when a user creates or
# writes to a project. ripgrep IS needed at runtime: the grep tool shells
# out to `rg` for content search. chromium pulls its own X11 / NSSS / ATK
# runtime libs as deps.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        chromium \
        curl \
        fontconfig \
        git \
        ghostscript \
        inkscape \
        nginx \
        perl \
        poppler-utils \
        procps \
        psmisc \
        python3-pygments \
        qpdf \
        ripgrep \
        supervisor \
        tini \
        wget \
        x11vnc \
        xvfb \
        websockify \
        fonts-liberation \
        fonts-noto-cjk \
    && ln -sf /usr/bin/chromium /usr/bin/chromium-browser \
    && rm -rf /var/lib/apt/lists/*

# Pre-built artifacts from builder stages
COPY --from=backend-builder /opt/venv-sigma /opt/venv-sigma
COPY --from=backend-builder /opt/venv-jupyter /opt/venv-jupyter
COPY --from=backend-builder /usr/local/texlive /usr/local/texlive
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Backend Python source (Python has no compiled artifact — source IS runtime)
COPY backend ./backend
COPY docker ./docker

RUN chmod +x /app/docker/entrypoint.sh \
    && cp /app/docker/nginx.conf /etc/nginx/conf.d/default.conf \
    && rm -f /etc/nginx/sites-enabled/default

EXPOSE 3000

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-c", "/app/docker/supervisord.conf"]
