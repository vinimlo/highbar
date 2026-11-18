# The image that records docs/assets/demo.gif from docs/demo/demo.tape.
#
#   sh docs/demo/record.sh
#
# It installs this checkout, so the demo shows the code in the tree.
FROM ghcr.io/charmbracelet/vhs:v0.9.0

COPY --from=ghcr.io/astral-sh/uv:0.8.0 /uv /usr/local/bin/uv
COPY . /src
RUN uv venv --python 3.12 /opt/highbar \
    && uv pip install --python /opt/highbar/bin/python "/src[quickstart]" \
    && rm -rf /src
ENV PATH="/opt/highbar/bin:${PATH}"
