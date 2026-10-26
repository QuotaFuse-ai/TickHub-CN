FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# pyproject's data-files entry ships web/admin/index.html, so the build needs it
COPY web ./web
RUN pip install --prefix=/install ".[admin]"

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    RUN_MODE=http \
    LOG_LEVEL=info
WORKDIR /app
COPY --from=build /install /usr/local
COPY field_map.toml model_gateway.toml ./
COPY config ./config
COPY web ./web
RUN useradd --create-home --uid 10001 tickhub \
    && mkdir -p /app/var/archive /app/logs \
    && chown -R tickhub:tickhub /app
USER tickhub
EXPOSE 8000
ENTRYPOINT ["th-cn"]
CMD ["start-all", "--host", "0.0.0.0", "--port", "8000"]
