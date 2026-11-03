FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py agent.py knowledge.py ./
COPY data ./data
COPY web ./web
COPY *.html *.md *.csv ./
ENV HOST=0.0.0.0 PORT=8765
EXPOSE 8765
CMD ["python", "main.py"]
