FROM python:3.13-slim

RUN useradd --system --uid 10001 savekeeper \
    && mkdir /data && chown savekeeper /data
WORKDIR /app
COPY savestore.py server.py ./

ENV SAVEKEEPER_DATA=/data \
    SAVEKEEPER_PORT=8765 \
    PYTHONUNBUFFERED=1
USER savekeeper
VOLUME /data
EXPOSE 8765

CMD ["python", "server.py"]
