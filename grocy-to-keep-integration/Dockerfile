FROM python:3.12-slim

# Grocy sits behind Cloudflare, which 403s some default Python user agents.
# grocy_lists.py sends an explicit UA; this just keeps the default sane.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grocy_lists.py .

# The worker is stateless now that Google Keep support is gone — all state
# lives in Grocy and Home Assistant. No volume needed.
RUN useradd --create-home --uid 1000 app
USER app

ENTRYPOINT ["python", "/app/grocy_lists.py"]
CMD ["sync"]
