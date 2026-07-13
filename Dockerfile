FROM python:3.11-slim

WORKDIR /code

COPY ./requirements.txt /code/requirements.txt

RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

COPY . /code/

ENV PORT=8080

# ✅ --proxy-headers: Cloud Run は TLS ターミネーションプロキシなので必須
#    X-Forwarded-For / X-Forwarded-Proto を正しく処理するため
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
