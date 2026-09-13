FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
ENV POLL_INTERVAL=600
ENV DB_PATH=/app/data/rnz.db
ENV PORT=8080

EXPOSE 8080

CMD ["python", "app.py"]
