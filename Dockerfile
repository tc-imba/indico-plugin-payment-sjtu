FROM getindico/indico:latest

WORKDIR /opt
RUN set -ex; \
    wget https://github.com/facebook/watchman/releases/download/v2023.05.22.00/watchman-v2023.05.22.00-linux.zip; \
    unzip watchman-v2023.05.22.00-linux.zip; \
    cd watchman-v2023.05.22.00-linux; \
    mkdir -p /usr/local/{bin,lib} /usr/local/var/run/watchman; \
    cp bin/* /usr/local/bin; \
    cp lib/* /usr/local/lib; \
    chmod 755 /usr/local/bin/watchman; \
    chmod 2777 /usr/local/var/run/watchman; \
    cd ..; \
    rm -rf watchman-v2023.05.22.00-linux*;

WORKDIR /opt/indico

ENV VIRTUAL_ENV=/opt/indico/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN pip install indico-plugins pywatchman

COPY . /opt/indico-plugin-payment-sjtu/
RUN pip install -e /opt/indico-plugin-payment-sjtu

RUN bash -c "echo DEBUG=True >> /opt/indico/etc/indico.conf"

