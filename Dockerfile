# Backend image (Plan unit #22).
#
# python:3.13-slim rather than alpine: pandas/openpyxl/psycopg2-binary all
# ship manylinux wheels that alpine's musl libc can't use, so alpine would
# mean compiling numpy/pandas from source on every build.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Requirements are copied and installed before the source so a code-only
# change reuses the (slow) dependency layer from cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
