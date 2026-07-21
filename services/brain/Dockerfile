# Dockerfile for the FRIDAY Brain Service (local development only)
# This image is NOT intended for production deployment.

# Use a stable, slim Python base image. Python 3.11 is chosen for compatibility
# with project dependencies and a lean footprint.
FROM python:3.11-slim-buster

# Prevent Python from writing .pyc files to disc and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Create a non-root user and group
RUN adduser --system --group friday
USER friday

# Set the working directory
WORKDIR /app

# Create the services/brain directory structure within the WORKDIR
RUN mkdir -p services/brain

# Copy only necessary files for the 'friday-brain' package to leverage Docker cache.
# This includes pyproject.toml and the source directory for installation via pip.
COPY --chown=friday:friday services/brain/pyproject.toml ./services/brain/
COPY --chown=friday:friday services/brain/src ./services/brain/src

# Install the 'friday-brain' service and its runtime dependencies.
# The 'pip install .' command will read services/brain/pyproject.toml and install
# dependencies specified under '[project].dependencies'.
# It will NOT install 'optional-dependencies' like 'dev' or 'test'.
RUN pip install --no-cache-dir ./services/brain

# Expose the port FastAPI runs on
EXPOSE 8000

# Command to run the application using Uvicorn
# The --host 0.0.0.0 is crucial for it to be accessible inside the container from other containers/host
CMD ["python", "-m", "uvicorn", "friday_brain.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]