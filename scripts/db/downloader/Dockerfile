FROM python:3.11-slim

LABEL maintainer="bharatradar"
LABEL description="BharatRadar Schedule Downloader - Downloads flight schedules from FlightRadar24"

WORKDIR /opt/bharatradar/flight_radar

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "route_schedule_downloader.py"]