FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates unzip tini libicu-dev \
 && rm -rf /var/lib/apt/lists/*

ARG SLSKD_VERSION=0.22.5

RUN mkdir -p /app/slskd \
 && curl -L -o /tmp/slskd.zip \
    "https://github.com/slskd/slskd/releases/download/${SLSKD_VERSION}/slskd-${SLSKD_VERSION}-linux-x64.zip" \
 && unzip /tmp/slskd.zip -d /app/slskd \
 && chmod +x /app/slskd/slskd \
 && rm /tmp/slskd.zip

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py start.sh slskd.yml ./
RUN chmod +x start.sh

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["./start.sh"]
