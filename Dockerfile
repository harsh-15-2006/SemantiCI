FROM python:3.12-slim

# git is needed to clone submitted repositories
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV SEMANTICI_HOME=/data
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "semantici.web:app", "--host", "0.0.0.0", "--port", "8000"]
