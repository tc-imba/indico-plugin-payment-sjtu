# This file is part of the Indico plugins.
# Copyright (C) 2002 - 2023 CERN
#
# The Indico plugins are free software; you can redistribute
# them and/or modify them under the terms of the MIT License;
# see the LICENSE file for more details.

from itertools import chain
from hashlib import md5
import base64
import xmltodict
from uuid import UUID

import requests
from flask import flash, redirect, request
from flask_pluginengine import current_plugin
from werkzeug.exceptions import BadRequest

from indico.modules.events.payment.models.transactions import TransactionAction
from indico.modules.events.payment.notifications import notify_amount_inconsistency
from indico.modules.events.payment.util import register_transaction
from indico.modules.events.registration.models.registrations import Registration
from indico.web.flask.util import url_for
from indico.web.rh import RH

from indico_payment_sjtu import _

IPN_VERIFY_EXTRA_PARAMS = (('cmd', '_notify-validate'),)

sjtu_transaction_action_mapping = {'Completed': TransactionAction.complete,
                                   'Denied': TransactionAction.reject,
                                   'Pending': TransactionAction.pending}


class RHSJTUIPN(RH):
    """Process the notification sent by the PayPal"""

    CSRF_ENABLED = False

    def _process_args(self):
        self.sign = request.args['sign']
        self.raw_data = request.args['data']
        data = xmltodict.parse(self.raw_data)
        self.payment_result = data["payResult"]
        self.token = str(UUID(bytes=base64.b64decode(self.payment_result["billno"])))
        self.registration = Registration.query.filter_by(uuid=self.token).first()
        if not self.registration:
            raise BadRequest

    def _process(self):
        # self._verify_business()
        verify_params = list(chain(IPN_VERIFY_EXTRA_PARAMS, request.form.items()))
        result = requests.post(current_plugin.settings.get('url'), data=verify_params).text
        if result != 'VERIFIED':
            current_plugin.logger.warning("Paypal IPN string %s did not validate (%s)", verify_params, result)
            return
        if self._is_transaction_duplicated():
            current_plugin.logger.info("Payment not recorded because transaction was duplicated\nData received: %s",
                                       request.form)
            return
        payment_status = request.form.get('payment_status')
        if payment_status == 'Failed':
            current_plugin.logger.info("Payment failed (status: %s)\nData received: %s", payment_status, request.form)
            return
        if payment_status == 'Refunded' or float(request.form.get('mc_gross')) <= 0:
            current_plugin.logger.warning("Payment refunded (status: %s)\nData received: %s",
                                          payment_status, request.form)
            return
        if payment_status not in sjtu_transaction_action_mapping:
            current_plugin.logger.warning("Payment status '%s' not recognized\nData received: %s",
                                          payment_status, request.form)
            return
        self._verify_amount()
        register_transaction(registration=self.registration,
                             amount=float(request.form['mc_gross']),
                             currency=request.form['mc_currency'],
                             action=sjtu_transaction_action_mapping[payment_status],
                             provider='sjtu',
                             data=request.form)

    def _verify_sign(self):
        sysid = current_plugin.event_settings.get(self.registration.registration_form.event, 'sysid')
        subsysid = current_plugin.event_settings.get(self.registration.registration_form.event, 'subsysid')
        cert = current_plugin.settings.get('cert')
        md5_string = sysid + subsysid + cert + self.raw_data
        sign = md5(md5_string.encode("utf-8")).hexdigest()
        return sign == self.sign

    def _verify_business(self):
        expected = current_plugin.event_settings.get(self.registration.registration_form.event, 'business').lower()
        candidates = {request.form.get('business', '').lower(),
                      request.form.get('receiver_id', '').lower(),
                      request.form.get('receiver_email', '').lower()}
        if expected in candidates:
            return True
        current_plugin.logger.warning("Unexpected business: %s not in %r (request data: %r)", expected, candidates,
                                      request.form)
        return False

    def _verify_amount(self):
        expected_amount = float(self.registration.price)
        currency = self.registration.currency
        amount = float(self.payment_result["billamt"])
        # currency = request.form['mc_currency']
        if expected_amount == amount:
            return True
        current_plugin.logger.warning("Payment doesn't match event's fee: %s %s != %s %s",
                                      amount, currency, expected_amount, currency)
        # notify_amount_inconsistency(self.registration, amount, currency)
        return False

    def _is_transaction_duplicated(self):
        transaction = self.registration.transaction
        if not transaction or transaction.provider != 'sjtu':
            return False
        return transaction.data['trade_no'] == self.payment_result["trade_no"]


class RHSJTUSuccess(RHSJTUIPN):
    """Confirmation message after successful payment"""

    def _process(self):
        if not self._verify_sign():
            flash(_('Payment sign error.'), 'error')
        elif not self._verify_amount():
            flash(_('Payment amount error.'), 'error')
        elif self._is_transaction_duplicated():
            flash(_('Payment transaction duplicated.'), 'warning')
        else:
            payment_status = "Completed"
            register_transaction(registration=self.registration,
                                 amount=float(self.payment_result["billamt"]),
                                 currency=self.registration.currency,
                                 action=sjtu_transaction_action_mapping[payment_status],
                                 provider='sjtu',
                                 data=self.payment_result)
            flash(_('Your payment request has been processed.'), 'success')
        return redirect(url_for('event_registration.display_regform', self.registration.locator.registrant))


class RHSJTUCancel(RHSJTUIPN):
    """Cancellation message"""

    def _process(self):
        flash(_('You cancelled the payment process.'), 'info')
        return redirect(url_for('event_registration.display_regform', self.registration.locator.registrant))
