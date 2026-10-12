FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    net-tools \
    inetutils-ping \
    inetutils-traceroute \
    nano \
    vim-nox \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

RUN chmod +x /app/init.sh
RUN chmod +x /app/entrypoint.sh
# A checkout on Windows writes these two with CRLF unless it is told otherwise
# (see .gitattributes). Linux then looks for an interpreter called "/bin/sh\r"
# and refuses to run the file at all, so the carriage returns are taken out
# here as well: the image works whatever the checkout looks like.
RUN sed -i 's/\r$//' /app/init.sh /app/entrypoint.sh
ARG SERVER_ADDRESS
ARG DEBUG_MODE
ENTRYPOINT ["/app/entrypoint.sh"]

CMD ["/app/init.sh"]