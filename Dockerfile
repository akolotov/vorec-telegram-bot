FROM python:3.12-slim

WORKDIR /app

COPY requirements.container.txt .
RUN pip install --no-cache-dir -r requirements.container.txt

COPY bot.py ./
COPY vorec ./vorec

CMD ["python", "bot.py"]
