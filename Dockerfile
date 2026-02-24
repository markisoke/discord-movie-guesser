FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py ./

# /data holds the SQLite database, screenshots, and logs persistently
VOLUME ["/data"]

CMD ["python", "-u", "bot.py"]
