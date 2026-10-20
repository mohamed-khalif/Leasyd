# The hot store server (server.py, docs/hot-store.md) for Fargate (arm64). infra/deploy-phaseH1.sh puts
# this file, server.py and the query modules it reuses (services/compaction, shared/limits.py) in one
# folder and builds it in CodeBuild.
ARG BASE=public.ecr.aws/docker/library/python:3.12-slim-bookworm
FROM ${BASE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY *.py ./
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "server.py"]
