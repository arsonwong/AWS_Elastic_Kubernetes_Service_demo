FROM python:3.11-slim
WORKDIR /app

# Install runtime deps first (keeps layer cache)
COPY app/ /app/

# Install your local packages
RUN pip install --no-cache-dir numpy
RUN pip install --no-cache-dir boto3

# Your scripts/app
CMD ["python", "main.py"]
