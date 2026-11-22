FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY stream_gateway.py ./

EXPOSE 8790

CMD ["python3", "stream_gateway.py"]
