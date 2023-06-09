FROM getindico/indico:latest

RUN set -eux; \
	apt-get update; \
	apt-get install -y --no-install-recommends inotify-tools; \
    rm -rf /var/lib/apt/lists/*;

WORKDIR /opt/indico

ENV VIRTUAL_ENV=/opt/indico/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN pip install indico-plugins

COPY . /opt/indico-plugin-payment-sjtu/
RUN pip install -e /opt/indico-plugin-payment-sjtu

RUN bash -c "echo DEBUG=True >> /opt/indico/etc/indico.conf"

