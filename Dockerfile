FROM python:3.11-slim

WORKDIR /app

# Copy files
COPY requirements.txt .
COPY RickyBobby.py .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run bot
CMD ["python", "RickyBobby.py"]
