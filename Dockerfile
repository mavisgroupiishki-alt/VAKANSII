FROM python:3.12-slim

WORKDIR /app

# Системные зависимости (минимум для aiogram + sqlite)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Сначала зависимости — для кэша слоёв
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

CMD ["python", "bot.py"]
