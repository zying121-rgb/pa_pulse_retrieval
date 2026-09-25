FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home pulse && mkdir /data && chown pulse /data
COPY pulse_service.py sources.json ./
USER pulse
ENV HOST=0.0.0.0 PORT=8080 PULSE_DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "pulse_service.py"]
