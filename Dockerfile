# Same base image the commits were labeled in (its Python and SQLite decide
# which sqlglot tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The packages sqlglot's tests import, at the versions in requirements.lock.
RUN pip install --no-cache-dir --disable-pip-version-check \
    duckdb==2.0.0 pandas==3.1.1 numpy==2.5.5 python-dateutil==4.0.0 \
    pytz==2026.6 tzdata==2026.6 setuptools==84.2.1 typing_extensions==4.16.0

COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /opt/sqlglot \
 && (python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests || true) \
 && chmod -R a+rX /opt/sqlglot

COPY selector.py /app/selector.py
COPY blast /app/blast
COPY map /app/map
RUN (python -m compileall -q /app || true) && chmod -R a+rX /app

ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "/app/selector.py"]
