FROM python:3.13-slim

# Runtime dependencies are installed by trusted setup, never by an agent job.
RUN python -m pip install --no-cache-dir pytest==8.4.2 \
    && groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid 1000 --no-create-home sandbox

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /workspace
USER 1000:1000
CMD ["python", "--version"]
