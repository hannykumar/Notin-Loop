FROM python:3.12-slim

ARG TARGETARCH
ARG KUBECTL_VERSION=v1.33.4

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV SRE_DEFAULT_TOOL_TRANSPORT=mcp
ENV SRE_BIND_HOST=0.0.0.0
ENV DATABASE_URL=sqlite:////app/runtime.db

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r /app/requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

RUN chmod +x /app/scripts/start_stack.sh

EXPOSE 8090

CMD ["/app/scripts/start_stack.sh"]
