FROM python:3.13-alpine

LABEL org.opencontainers.image.authors="Aarno Aukia <aarno.aukia@vshn.ch>"

WORKDIR /usr/src/app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY get_certificate.py .
RUN uv run python get_certificate.py

COPY templates ./templates
COPY app.py ./

USER 1001
CMD ["uv", "run", "python", "app.py"]
EXPOSE 8080
