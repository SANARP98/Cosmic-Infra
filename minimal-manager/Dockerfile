FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app.py /app/app.py

RUN pip install --no-cache-dir fastapi uvicorn

EXPOSE 12000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "12000"]