FROM python:3.11-slim

# Install FFmpeg for audio processing
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy files
COPY requirements.txt .
COPY RickyBobby.py .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run bot
CMD ["python", "RickyBobby.py"]
