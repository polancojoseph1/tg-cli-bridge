FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Create memory directory
RUN mkdir -p /memories

EXPOSE 8585

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8585"]
