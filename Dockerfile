# App:        docker build -t andon . && docker run -p 8501:8501 -e DATABASE_URL=... andon
# Connector:  docker run --network host -e DATABASE_URL=... -v "$(pwd)/collector/config.yaml:/app/collector/config.yaml" andon \
#               python collector/collector.py --config collector/config.yaml
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY collector/requirements.txt ./collector-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r collector-requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
