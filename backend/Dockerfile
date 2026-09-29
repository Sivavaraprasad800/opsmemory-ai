# OpsMemory AI — backend image.
#
# Deliberately a single stage: the app is pure Python with no build step, so a multi-stage
# build would add complexity without removing anything. The test dependencies stay in
# requirements.txt so that a single install both runs the platform and runs the suite.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy requirements first so a code change does not invalidate the dependency layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The SQLite default lives here and is mounted as a volume by docker-compose, so the
# catalogue and any retained state survive a container rebuild.
RUN mkdir -p /app/data

EXPOSE 8000

# The schema and the seeded catalogue are created by the app's lifespan hook on first boot,
# so there is no separate migration step to run.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
