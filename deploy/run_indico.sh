#!/bin/bash

/opt/indico/set_user.sh
. /opt/indico/.venv/bin/activate

psql $PGDATABASE -lqt | cut -d \| -f 1 | grep -qw $PGDATABASE

until [ $? -eq 0 ]; do
    sleep 1
    psql $PGDATABASE -lqt | cut -d \| -f 1 | grep -qw $PGDATABASE
done

psql -c 'SELECT COUNT(*) FROM events.events'

if [ $? -eq 1 ]; then
    echo 'Preparing DB...'
    if [ "$USE_EXTERNAL_DB" == 'y' ]; then
        echo 'Using external database...'
        echo 'CREATE EXTENSION unaccent;' | psql
        echo 'CREATE EXTENSION pg_trgm;' | psql
    else
        echo 'Using PostgreSQL container...'
        echo 'CREATE EXTENSION unaccent;' | psql -U postgres
        echo 'CREATE EXTENSION pg_trgm;' | psql -U postgres
    fi
    indico db prepare
fi

#echo 'Watch file changes'
#echo -e 'fs.inotify.max_queued_events = 524288\nfs.inotify.max_user_instances = 524288\nfs.inotify.max_user_watches = 524288' > /etc/sysctl.conf
#sysctl -p
#bash /opt/indico/watch.sh touch /opt/indico/indico.wsgi &

echo 'Starting Indico...'
#watchman-make -p '/opt/indico-plugin-payment-sjtu/indico_payment_sjtu/**/*' -s 1 -r 'touch /opt/indico/indico.wsgi'
uwsgi /etc/uwsgi.ini
