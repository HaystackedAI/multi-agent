# One image, two roles:
#   - AgentCore Runtime container build (ARM64):  docker buildx build --platform linux/arm64 -t chaser .
#     Default CMD serves the AgentCore HTTP contract from main.py on 8080.
#   - Web UI on AWS App Runner (x86_64, built by infra/web.yaml via CodeBuild); the service overrides
#     the start command with: uvicorn app.server:app --host 0.0.0.0 --port 8080
# The base image comes from ECR Public to avoid Docker Hub pull limits in CI.
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHASER_DB_PATH=/tmp/chaser.db \
    CHASER_SESSIONS_DIR=/tmp/sessions \
    DEMO_TODAY=2026-09-12

WORKDIR /app

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY src ./src
COPY app ./app
COPY data ./data
RUN pip install --no-cache-dir --no-deps -e .

RUN useradd --create-home --uid 10001 chaser && chown -R chaser:chaser /app
USER chaser

EXPOSE 8080
CMD ["python", "main.py"]
