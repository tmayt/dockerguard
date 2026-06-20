FROM gitea.tmayt.ir/thaiostream/python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .

RUN apt-get update && apt-get install -y curl

RUN pip install --no-cache-dir -r requirements.txt --index-url https://mirror2.chabokan.net/pypi/simple/ --trusted-host mirror2.chabokan.net

# Copy source
COPY . .

# Create log dir
RUN mkdir -p logs

# Expose dashboard port
EXPOSE 8080

# Run
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--reload"]