FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright U2NET_HOME=/models
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*
# recorte do jogador: opcional. Se falhar, o deploy segue e os cards usam a foto inteira.
RUN (pip install --no-cache-dir "rembg[cpu]==2.0.85" \
     && python -c "from rembg import new_session; new_session('isnet-general-use')") \
    || echo "AVISO: recorte (rembg) indisponível nesta imagem"
COPY app ./app
COPY static ./static
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
