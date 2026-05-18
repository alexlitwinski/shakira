FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION=1.0.0
ARG BUILD_ARCH

LABEL io.hass.version="${BUILD_VERSION}"
LABEL io.hass.type="app"
LABEL io.hass.arch="${BUILD_ARCH}"

WORKDIR /app

RUN apk add --no-cache \
    python3 \
    py3-pip

COPY requirements.txt .

RUN python3 -m venv /opt/shakira \
 && /opt/shakira/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/opt/shakira/bin:${PATH}"

COPY run.sh /run.sh
RUN chmod a+x /run.sh

COPY app ./app

CMD [ "/run.sh" ]
