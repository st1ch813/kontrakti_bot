FROM python:3.11-slim

WORKDIR /app

# Отключаем буферизацию вывода Python для мгновенного логирования
ENV PYTHONUNBUFFERED=1
ENV PORT=5000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["python", "bot.py"]
