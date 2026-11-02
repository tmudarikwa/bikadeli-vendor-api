FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DJANGO_SETTINGS_MODULE=vendor_api.settings

WORKDIR /app
COPY vendor_api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --retries=5 --timeout=120 -r /app/requirements.txt

COPY --chown=10001:10001 common-services/python/bikadeli_common /app/bikadeli_common
COPY --chown=10001:10001 vendor_api/manage.py /app/manage.py
COPY --chown=10001:10001 vendor_api/vendor_api /app/vendor_api
COPY --chown=10001:10001 vendor_api/vendor_catalog /app/vendor_catalog

RUN adduser --system --uid 10001 --no-create-home app \
    && mkdir -p /app/media /app/private-kyc \
    && chown -R 10001:10001 /app/media /app/private-kyc
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/', timeout=4).read()"]
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=2", "--timeout=30", "vendor_api.wsgi:application"]
