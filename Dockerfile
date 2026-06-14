FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 
WORKDIR /app
RUN apt-get update && apt-get install -y \ gcc \ curl \ && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \ pip install --no-cache-dir -r requirements.txt 
COPY . . 
EXPOSE 8000
RUN useradd -m prismsre USER prismsre 
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

