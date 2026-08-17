# Model Council over HTTP: one deployment, one set of keys, a team of callers.
#
#   docker build -t model-council -f deploy/Dockerfile .
#   docker run -d --name model-council -p 8000:8000 \
#     -v /etc/model-council/config.json:/config/config.json:ro \
#     -e COUNCIL_CONFIG=/config/config.json \
#     -e COUNCIL_ALLOW=10.20.0.0/16 \
#     model-council
#
# The provider keys stay in the mounted file and in this container. Nobody who
# calls the server needs one, which is the entire point — and the reason
# COUNCIL_ALLOW has no default here: the container binds 0.0.0.0, and the server
# will not serve a network until you have said which one.

FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
COPY --from=build /install /usr/local

# Nothing in here needs root, and a process holding provider keys is a good
# reason to be sure of that.
RUN useradd --system --no-create-home --uid 10001 council
USER council

ENV COUNCIL_HTTP=1 \
    COUNCIL_HTTP_HOST=0.0.0.0 \
    COUNCIL_HTTP_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# /healthz answers 503 until at least one member is configured, so a container
# that came up with an unreadable or empty config file reports unhealthy instead
# of sitting there looking fine and failing every tool call. The probe runs on
# loopback, which the allowlist admits unconditionally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz')"

ENTRYPOINT ["model-council-mcp"]
