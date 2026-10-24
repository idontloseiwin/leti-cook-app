FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 PYTHON_DOTENV_DISABLED=1 HOME=/var/lib/leti-cook
WORKDIR /opt/leti-cook
# Every package, transitive ones included, is pinned by version and hash.
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock && \
    groupadd --gid 10001 lic && useradd --uid 10001 --gid lic --no-create-home --home-dir /var/lib/leti-cook lic && \
    install -d -m 0700 -o lic -g lic /var/lib/leti-cook /var/lib/leti-cook/access /var/lib/leti-cook/configuration
COPY ./ ./
USER 10001:10001
CMD ["python", "ops/self_hosted_container.py"]
