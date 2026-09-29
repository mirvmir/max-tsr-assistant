FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
RUN pip install --no-cache-dir --no-build-isolation --no-deps -e . \
    && useradd --system --uid 10001 --create-home tsr \
    && mkdir -p /var/lib/tsr/blobs \
    && chown -R tsr:tsr /var/lib/tsr
USER tsr
EXPOSE 8080
CMD ["tsr", "serve"]
