FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY duunitutka.py .

# One linear pass per container start — Railway Cron schedules the runs.
CMD ["python", "duunitutka.py"]
