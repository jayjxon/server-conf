FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY catalog.py anime_library.py ./

EXPOSE 8787

CMD ["python3", "anime_library.py", "--host", "0.0.0.0", "--port", "8787"]
