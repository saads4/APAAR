FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app app
COPY migrations migrations
COPY alembic.ini entrypoint.sh ./
RUN chmod +x entrypoint.sh && mkdir -p data
EXPOSE 8000
CMD ["./entrypoint.sh"]
