# syntax=docker/dockerfile:1
# Stdlib-only mock — no pip install, no network dependency.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY app.py .
EXPOSE 8000
CMD ["python", "app.py"]
