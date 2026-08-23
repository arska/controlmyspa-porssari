FROM python:3.14-alpine

LABEL org.opencontainers.image.authors="Aarno Aukia <aarno.aukia@vshn.ch>"

WORKDIR /usr/src/app

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

# use the venv python for all subsequent commands
ENV PATH="/usr/src/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY get_certificate.py .
RUN python get_certificate.py

COPY templates ./templates
# Every module app.py imports, or the container dies at startup.
# test_dockerfile.py keeps this list in step with the imports.
COPY app.py metrics.py pricing.py scheduling.py storage.py thermal.py ./

USER 1001
CMD ["python", "app.py"]
EXPOSE 8080
