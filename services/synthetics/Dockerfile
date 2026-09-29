# Browser checks (browser.py): Python + Playwright's headless Chromium, for Lambda (arm64).
# Built by infra/deploy-phaseS1.sh through CodeBuild (no Docker needed where you deploy from).
ARG BASE=public.ecr.aws/docker/library/python:3.12-slim-bookworm
FROM ${BASE}

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir playwright==1.56.0 awslambdaric==4.1.0 \
 && playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache

WORKDIR /var/task
COPY safety.py browser.py ./
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["browser.handler"]
