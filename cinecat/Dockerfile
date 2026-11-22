FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY cinecat_library.py ./

EXPOSE 8788

CMD ["python3", "cinecat_library.py"]
