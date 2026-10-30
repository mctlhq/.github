# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app server.py agent.py smoke_anthropic.py ./
USER 10001
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PORT=8001 REPLICA=a
EXPOSE 8001
CMD ["python", "server.py"]
