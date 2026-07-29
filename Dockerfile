# Minimal image for running the Telegram bot (Python only, MySQL is external)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY modules ./modules
COPY resources ./resources
COPY .env.example ./.env.example

# FOGMOE OAuth callback server
EXPOSE 18765

CMD ["python", "-u", "modules/main.py"]
