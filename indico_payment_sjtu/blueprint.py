# This file is part of the Indico plugins.
# Copyright (C) 2002 - 2023 CERN
#
# The Indico plugins are free software; you can redistribute
# them and/or modify them under the terms of the MIT License;
# see the LICENSE file for more details.

from indico.core.plugins import IndicoPluginBlueprint

from indico_payment_sjtu.controllers import RHSJTUSuccess, RHSJTUQuery, RHSJTUInvoice, RHSJTUCallback


blueprint = IndicoPluginBlueprint(
    'payment_sjtu', __name__,
    url_prefix=''
)

blueprint.add_url_rule('/event/<int:event_id>/registrations/<int:reg_form_id>/payment/sjtu/query', 'query', RHSJTUQuery, methods=('GET', 'POST'))
blueprint.add_url_rule('/event/<int:event_id>/registrations/<int:reg_form_id>/payment/sjtu/success', 'success', RHSJTUSuccess, methods=('GET', 'POST'))
blueprint.add_url_rule('/event/<int:event_id>/registrations/<int:reg_form_id>/payment/sjtu/invoice', 'invoice', RHSJTUInvoice, methods=('GET',))
blueprint.add_url_rule('/payment/sjtu/callback', 'callback', RHSJTUCallback, methods=('GET', 'POST'))

# Used by PayPal to send an asynchronous notification for the transaction (pending, successful, etc)
# blueprint.add_url_rule('/ipn', 'notify', RHSJTUIPN, methods=('POST',))


