FROM index.xxxxx.com/library/alpine:3.2

ENV API_KEY cloudxns-api-key
ENV SECRET_KEY cloudxns-secret-key
ENV DOMAIN cloudxns-domain
ENV HOSTS hosts
ENV GROUP group
ENV MARATHON marathon

RUN apk add --update python py-pip openssl \
    && apk add --update --repository http://dl-3.alpinelinux.org/alpine/edge/testing \
    runit \
    && pip install requests sseclient restclient CloudXNS-API-SDK-Python --no-cache-dir --index-url http://pypi.xxxxx.com/prod/pypi/+simple/ --trusted-host pypi.xxxxx.com

COPY  . /marathon-cloudxns
WORKDIR /marathon-cloudxns

EXPOSE     8080
ENTRYPOINT [ "/marathon-cloudxns/run" ]
CMD        [ "sse", "-m", "$MARATHON", "--cloudxns-domain", "$DOMAIN", "--cloudxns-api-key", "$API_KEY", "--cloudxns-secret-key", "$SECRET_KEY", "--hosts", "$HOSTS", "--group", "$GROUP"]
