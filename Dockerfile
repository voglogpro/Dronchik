FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static
COPY geodata geodata
COPY main.py channels.default.json ./
# data/ — база сообщений и сессия Telegram; монтируйте сюда постоянное хранилище
RUN mkdir -p data
EXPOSE 8000
CMD ["python", "main.py"]
