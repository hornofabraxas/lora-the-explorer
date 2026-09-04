FROM python:3.12-slim

WORKDIR /app

# Install third-party dependencies first, from pyproject's own dependency list,
# so this heavy layer is cached across source-only changes (it only rebuilds when
# pyproject.toml itself changes). The deps are read straight from pyproject via
# tomllib (Python 3.11+ stdlib), not a duplicate requirements file, so they can
# never drift out of sync with the package metadata.
COPY pyproject.toml .
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/deps.txt \
    && pip install --no-cache-dir -r /tmp/deps.txt

# Now bring in the source and install just this package (dependencies already
# present, so --no-deps keeps this a fast, source-only layer). The resulting image
# is byte-for-byte equivalent to a plain `pip install .`; only the layer structure
# changes.
COPY src/ src/
RUN pip install --no-cache-dir --no-deps .

ENV LORA_INSTALL_METHOD=docker
ENV CONNECTION_TYPE=wifi
ENV COMPANION_HOST=
ENV COMPANION_PORT=4000
ENV HOME_LAT=0
ENV HOME_LON=0

EXPOSE 1492

CMD ["lora-explorer"]
