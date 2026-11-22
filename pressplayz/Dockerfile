FROM python:3.12-slim

# CNO's CDN requires HTTP/2 for media segments; Python's stdlib only does 1.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pressplayz_library.py ./

EXPOSE 8789

CMD ["python3", "pressplayz_library.py"]
