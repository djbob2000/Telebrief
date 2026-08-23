# Use Python 3.14 slim image — pinned for reproducibility
FROM mirror.gcr.io/library/python:3.14.3-slim

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies and Playwright Chromium with OS dependencies
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

# Copy application code
COPY . .

# Create non-root user
RUN useradd -r -u 1000 -s /usr/sbin/nologin telebrief

# Create necessary directories, auth storage, and set ownership
RUN mkdir -p logs sessions data /var/lib/telebrief/auth \
    && chown -R telebrief:telebrief logs sessions data /var/lib/telebrief/auth \
    && chmod 700 /var/lib/telebrief/auth

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV LOG_LEVEL=INFO

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import os, signal; os.kill(1, 0)"

# Switch to non-root user
USER telebrief

# Run the application
CMD ["python", "main.py"]
