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
from flask import flash, redirect, request, jsonify, session
from flask_pluginengine import current_plugin, render_plugin_template
from indico.modules.events.controllers.base import RHDisplayEventBase
from indico.modules.events.registration.controllers.display import RHRegistrationFormDisplayBase
from indico.modules.events.registration.util import get_event_regforms_registrations
from werkzeug.exceptions import BadRequest

from indico.modules.events.payment.models.transactions import TransactionAction
from indico.modules.events.payment.notifications import notify_amount_inconsistency
from indico.modules.events.payment.util import register_transaction
from indico.modules.events.registration.models.registrations import Registration, RegistrationState
from indico.web.flask.util import url_for
from indico.web.rh import RH

from indico_payment_sjtu import _
from indico_payment_sjtu.views import WPInvoice

IPN_VERIFY_EXTRA_PARAMS = (('cmd', '_notify-validate'),)

sjtu_transaction_action_mapping = {'Completed': TransactionAction.complete,
                                   'Denied': TransactionAction.reject,
                                   'Pending': TransactionAction.pending}


class RHSJTUBase(RH):
    """Process the notification sent by the PayPal"""

    CSRF_ENABLED = False

    def _init_registration(self, token):
        self.registration = Registration.query.filter_by(uuid=token).first()
        if not self.registration:
            raise BadRequest
        self.sysid = current_plugin.event_settings.get(self.registration.registration_form.event, 'sysid')
        self.subsysid = current_plugin.event_settings.get(self.registration.registration_form.event, 'subsysid')
        self.cert = current_plugin.settings.get('cert')

    # def _process(self):
    #     # self._verify_business()
    #     verify_params = list(chain(IPN_VERIFY_EXTRA_PARAMS, request.form.items()))
    #     result = requests.post(current_plugin.settings.get('url'), data=verify_params).text
    #     if result != 'VERIFIED':
    #         current_plugin.logger.warning("Paypal IPN string %s did not validate (%s)", verify_params, result)
    #         return
    #     if self._is_transaction_duplicated():
    #         current_plugin.logger.info("Payment not recorded because transaction was duplicated\nData received: %s",
    #                                    request.form)
    #         return
    #     payment_status = request.form.get('payment_status')
    #     if payment_status == 'Failed':
    #         current_plugin.logger.info("Payment failed (status: %s)\nData received: %s", payment_status, request.form)
    #         return
    #     if payment_status == 'Refunded' or float(request.form.get('mc_gross')) <= 0:
    #         current_plugin.logger.warning("Payment refunded (status: %s)\nData received: %s",
    #                                       payment_status, request.form)
    #         return
    #     if payment_status not in sjtu_transaction_action_mapping:
    #         current_plugin.logger.warning("Payment status '%s' not recognized\nData received: %s",
    #                                       payment_status, request.form)
    #         return
    #     self._verify_amount()
    #     register_transaction(registration=self.registration,
    #                          amount=float(request.form['mc_gross']),
    #                          currency=request.form['mc_currency'],
    #                          action=sjtu_transaction_action_mapping[payment_status],
    #                          provider='sjtu',
    #                          data=request.form)
    def _generate_sign(self, data):
        md5_string = self.sysid + self.subsysid + self.cert + data
        sign = md5(md5_string.encode("utf-8")).hexdigest()
        return sign

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

    def _verify_amount(self, amount):
        expected_amount = float(self.registration.price)
        currency = self.registration.currency
        # currency = request.form['mc_currency']
        if expected_amount == amount:
            return True
        current_plugin.logger.warning("Payment doesn't match event's fee: %s %s != %s %s",
                                      amount, currency, expected_amount, currency)
        # notify_amount_inconsistency(self.registration, amount, currency)
        return False

    def _is_transaction_duplicated(self, trade_no):
        transaction = self.registration.transaction
        if not transaction or transaction.provider != 'sjtu':
            return False
        return transaction.data['trade_no'] == trade_no

    def _register_transaction(self, payment_result):
        payment_status = "Completed"
        register_transaction(registration=self.registration,
                             amount=float(payment_result["billamt"]),
                             currency=self.registration.currency,
                             action=sjtu_transaction_action_mapping[payment_status],
                             provider='sjtu',
                             data=payment_result)

class RHSJTUSuccess(RHSJTUBase):
    """Confirmation message after successful payment"""

    def _process_args(self):
        self.sign = request.args['sign']
        self.raw_data = request.args['data']
        data = xmltodict.parse(self.raw_data)
        self.payment_result = data["payResult"]
        self.token = str(UUID(bytes=base64.b64decode(self.payment_result["billno"])))
        self._init_registration(self.token)

    def _process(self):
        if self._generate_sign(self.raw_data) != self.sign:
            flash(_('Payment sign error.'), 'error')
        elif not self._verify_amount(float(self.payment_result["billamt"])):
            flash(_('Payment amount error.'), 'error')
        elif self._is_transaction_duplicated(self.payment_result["trade_no"]):
            flash(_('Payment transaction duplicated.'), 'warning')
        else:
            self._register_transaction(self.payment_result)
            flash(_('Your payment request has been processed.'), 'success')
        return redirect(url_for('event_registration.display_regform', self.registration.locator.registrant))


class RHSJTUQuery(RHSJTUBase):
    def _process_args(self):
        self.sign = request.form['sign']
        self.raw_data = request.form['data']
        data = xmltodict.parse(self.raw_data)
        self.billinfo = data["billinfo"]
        self.token = str(UUID(bytes=base64.b64decode(self.billinfo["billno"])))
        self._init_registration(self.token)

    def _is_transaction_success_in_sjtu(self):
        query_url = f"{current_plugin.settings.get('url')}/portal/Query_PayQuery.action"
        billno = self.billinfo["billno"]
        sign = self._generate_sign(billno)
        params = {
            "sign": sign,
            "sysid": self.sysid,
            "subsysid": self.subsysid,
            "billno": billno,
        }
        print(query_url)
        response = requests.get(query_url, params=params)
        result = response.text
        at_pos = result.find("@")
        sign = result[:at_pos]
        raw_data = result[at_pos + 1:]
        if self._generate_sign(raw_data) != sign:
            return False
        data = xmltodict.parse(raw_data)
        print(data)
        returncode = data["QueryResult"]["State"]["returncode"]
        if returncode != "0000":
            return False
        if data["QueryResult"]["Billinfo"] is None:
            return False
        payment_results = data["QueryResult"]["Billinfo"]["billdetail"]
        if not isinstance(payment_results, list):
            payment_results = [payment_results]
        for payment_result in payment_results:
            if int(payment_result["paystate"]) == 4 and self._verify_amount(float(payment_result["billamt"])):
                payment_result.pop("paystate")
                self._register_transaction(payment_result)
                return True
        return False

    def _process(self):
        if self._generate_sign(self.raw_data) != self.sign:
            return jsonify(success=False)
        elif self.registration.state != RegistrationState.unpaid:
            return jsonify(success=False)
        elif self._is_transaction_success_in_sjtu():
            return jsonify(success=False)
        return jsonify(success=True)


# class RHSJTUInvoiceList(RHRegistrationFormDisplayBase):
#     @staticmethod
#     def _filter_payed_regforms(regform):
#         print(regform)
#
#         return True
#
#     def _process(self):
#         displayed_regforms, user_registrations = get_event_regforms_registrations(self.event, session.user,
#                                                                                   only_in_acl=self.is_restricted_access)
#         displayed_regforms = list(filter(self._filter_payed_regforms, displayed_regforms))
#
#         # if len(displayed_regforms) == 1:
#         #     return redirect(url_for('event_registration.display_regform', displayed_regforms[0]))
#         return WPInvoice.render_template('invoice_list.html', self.event,
#                                          regforms=displayed_regforms,
#                                          user_registrations=user_registrations,
#                                          is_restricted_access=self.is_restricted_access)
#
#         # return WPInvoice.render_template('invoice_list.html', self.event)
#

class RHSJTUCallback(RHSJTUBase):

    def _process(self):
        return jsonify(success=True)

# class RHSJTUCancel(RHSJTUIPN):
#     """Cancellation message"""
#
#     def _process(self):
#         flash(_('You cancelled the payment process.'), 'info')
#         return redirect(url_for('event_registration.display_regform', self.registration.locator.registrant))
