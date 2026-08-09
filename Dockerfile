FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/uploads

# compose 里 command 只写 uvicorn；entrypoint 负责启动前自动 alembic upgrade head
ENTRYPOINT ["sh", "-c", "alembic upgrade head && exec \"$@\"", "entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
