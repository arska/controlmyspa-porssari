FROM python:3.14-alpine

LABEL org.opencontainers.image.authors="Aarno Aukia <aarno.aukia@vshn.ch>"

WORKDIR /usr/src/app

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /bin/uv

# use the venv python for all subsequent commands
ENV PATH="/usr/src/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY get_certificate.py .
RUN python get_certificate.py

COPY templates ./templates
COPY app.py ./

USER 1001
CMD ["python", "app.py"]
EXPOSE 8080
