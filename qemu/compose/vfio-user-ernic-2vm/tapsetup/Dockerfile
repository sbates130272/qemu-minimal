# Builds the guest Ethernet path inside each ernic container's netns.
#
# Built by compose from this directory rather than pulled, for two reasons:
# the image is three lines and not worth publishing, and 32 containers each
# running `apk add` at start is exactly the kind of start-burst that already
# cost us twelve guests when `pipx install` raced to pypi.org. Compose builds
# it once and every sidecar reuses the layer.
FROM alpine:3.20
# AMD_CERT: optional base64-encoded CA cert appended before apk runs.
# Set via ALPINE_BUILD_ARGS on AMD corporate machines to trust the ZScaler MITM.
ARG AMD_CERT=""
RUN [ -z "$AMD_CERT" ] || printf '%s' "$AMD_CERT" | base64 -d \
        >> /etc/ssl/certs/ca-certificates.crt
RUN apk add --no-cache iproute2
COPY setup.sh /usr/local/bin/setup.sh
RUN chmod +x /usr/local/bin/setup.sh
ENTRYPOINT ["/usr/local/bin/setup.sh"]
