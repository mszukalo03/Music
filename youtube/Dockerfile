FROM --platform=$TARGETPLATFORM python:3.11-slim

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    libchromaprint-tools \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Use standard pip for maximum compatibility across ARM page sizes
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app ./app
COPY run.sh .
COPY manage_db.py .

# Ensure scripts are executable
RUN chmod +x run.sh manage_db.py

# Set python path
ENV PYTHONPATH=/app

# Default command
CMD ["./run.sh"]
