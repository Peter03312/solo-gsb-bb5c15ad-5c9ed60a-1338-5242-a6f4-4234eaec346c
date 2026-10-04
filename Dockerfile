FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOP_DB_PATH=/data/sop.db \
    SOP_HOST=0.0.0.0 \
    SOP_PORT=8080

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 持久化数据库文件（compose 中挂卷覆盖此目录）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import json,os,urllib.request;url='http://127.0.0.1:%s/health'%os.environ.get('SOP_PORT','8080');r=urllib.request.urlopen(url,timeout=2);raise SystemExit(0 if json.load(r)['status']=='ok' else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
