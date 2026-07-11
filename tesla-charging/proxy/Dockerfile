# Tesla's official command-signing HTTP proxy (github.com/teslamotors/vehicle-command).
# No official image is published, so we build our own.
FROM golang:1.24-alpine AS build
ARG VEHICLE_COMMAND_VERSION=v0.4.1
RUN apk add --no-cache git \
    && go install github.com/teslamotors/vehicle-command/cmd/tesla-http-proxy@${VEHICLE_COMMAND_VERSION}

FROM alpine:3.22
RUN apk add --no-cache ca-certificates
COPY --from=build /go/bin/tesla-http-proxy /usr/local/bin/tesla-http-proxy
USER 1001
ENTRYPOINT ["tesla-http-proxy"]
# TLS is mandatory; cert/key and the Fleet signing key are mounted by the
# Deployment (see ../deploy/proxy-deployment.yaml):
#   tesla-http-proxy -cert /tls/tls.crt -tls-key /tls/tls.key \
#     -key-file /keys/fleet-private-key.pem -host 0.0.0.0 -port 4443
