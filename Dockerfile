FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

ENV LORA_INSTALL_METHOD=docker
ENV CONNECTION_TYPE=wifi
ENV COMPANION_HOST=
ENV COMPANION_PORT=4000
ENV HOME_LAT=0
ENV HOME_LON=0

EXPOSE 1492

CMD ["lora-explorer"]
