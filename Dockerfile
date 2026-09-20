FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Unprivileged user; the container only makes outbound HTTP calls.
USER nobody

ENTRYPOINT ["python3", "main.py"]
