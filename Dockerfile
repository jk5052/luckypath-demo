FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LUCKYPATH_DATA_DIR=/app/data \
    LUCKYPATH_SOURCE_ARCHIVE=/app/public/luckypath-source.zip \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1

WORKDIR /app
COPY requirements-demo.txt ./
RUN pip install --no-cache-dir -r requirements-demo.txt
COPY . .
RUN pip install --no-cache-dir --no-deps .
RUN python deploy/build_source_archive.py \
    && useradd --uid 10001 --create-home demo \
    && mkdir -p /usr/local/lib/python3.12/site-packages/work/ephemeris \
    && chown -R demo:demo /usr/local/lib/python3.12/site-packages/work /app
USER demo
# Prove that sealed, packaged astronomy data works during the image build.
RUN python -m luckypath.webapp
EXPOSE 10000
CMD ["gunicorn", "--config", "deploy/gunicorn.conf.py", "luckypath.webapp:create_app()"]

