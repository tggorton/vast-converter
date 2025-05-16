# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set working directory
WORKDIR /app

# Install system dependencies
# - ffmpeg for video processing
# - fontconfig for font discovery by ffmpeg's drawtext
# - fonts-dejavu-core provides a good set of default fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    fonts-dejavu-core \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Expose the port the app runs on (though Vercel will likely override/manage this)
EXPOSE 5001

# Command to run the application using Gunicorn
# Vercel's build system might override this CMD, or it might use it.
# It expects the application to be served on 0.0.0.0 and a port Vercel provides via PORT env var.
# Gunicorn will bind to $PORT if set, otherwise 8000.
# We use app:app assuming your Flask instance is named 'app' in 'app.py'.
CMD ["gunicorn", "--bind", "0.0.0.0:${PORT:-5001}", "app:app"] 