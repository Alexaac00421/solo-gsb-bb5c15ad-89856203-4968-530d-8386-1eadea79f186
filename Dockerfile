FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 建表在应用 startup 事件中完成（含等待数据库就绪重试）。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
