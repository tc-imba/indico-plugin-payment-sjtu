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
import urllib.parse

import requests
from dict2xml import dict2xml
from flask import flash, redirect, request, jsonify, session
from flask_pluginengine import current_plugin, render_plugin_template
from indico.modules.events.controllers.base import RHDisplayEventBase
from indico.modules.events.payment import payment_event_settings
from indico.modules.events.registration.controllers.display import \
    RHRegistrationFormDisplayBase, RHRegistrationFormRegistrationBase
from indico.modules.events.registration.controllers.management import \
    RHManageRegFormBase, RHManageRegistrationBase
from indico.modules.events.registration.util import get_event_regforms_registrations
from indico.web.util import jsonify_data, jsonify_template
from werkzeug.exceptions import BadRequest

from indico.modules.events.payment.models.transactions import TransactionAction
from indico.modules.events.payment.notifications import notify_amount_inconsistency
from indico.modules.events.payment.util import register_transaction
from indico.modules.events.registration.models.registrations import Registration, \
    RegistrationState
from indico.web.flask.util import url_for
from indico.web.rh import RH

from indico_payment_sjtu import _
from indico_payment_sjtu.util import uuid_to_billno
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
        self._init_plugin_settings()

    def _init_plugin_settings(self):
        self.sysid = current_plugin.event_settings.get(
            self.registration.registration_form.event, 'sysid')
        self.subsysid = current_plugin.event_settings.get(
            self.registration.registration_form.event, 'subsysid')
        self.cert = current_plugin.settings.get('cert')

    def _generate_sign(self, data):
        md5_string = self.sysid + self.subsysid + self.cert + data
        sign = md5(md5_string.encode("utf-8")).hexdigest()
        return sign

    def _verify_business(self):
        expected = current_plugin.event_settings.get(
            self.registration.registration_form.event, 'business').lower()
        candidates = {request.form.get('business', '').lower(),
                      request.form.get('receiver_id', '').lower(),
                      request.form.get('receiver_email', '').lower()}
        if expected in candidates:
            return True
        current_plugin.logger.warning(
            "Unexpected business: %s not in %r (request data: %r)", expected,
            candidates,
            request.form)
        return False

    def _verify_amount(self, amount):
        expected_amount = float(self.registration.price)
        currency = self.registration.currency
        # currency = request.form['mc_currency']
        if expected_amount == amount:
            return True
        current_plugin.logger.warning(
            "Payment doesn't match event's fee: %s %s != %s %s",
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

    def _query_sjtu_portal(self, query_url, data, method="GET", unquote=False):
        current_plugin.logger.info("Send query to %s [%s]: %s", query_url, method, data)
        if method == "GET":
            response = requests.get(query_url, params=data)
        elif method == "POST":
            response = requests.post(query_url, data=data)
        else:
            current_plugin.logger.error("HTTP method error: %s", method)
            return None
        result = response.text
        current_plugin.logger.info("Receive data: %s", result)
        at_pos = result.find("@")
        sign = result[:at_pos]
        raw_data = result[at_pos + 1:]
        if unquote:
            raw_data = urllib.parse.unquote_plus(raw_data)
            current_plugin.logger.info("Unquote data: %s", raw_data)
        my_sign = self._generate_sign(raw_data)
        if my_sign != sign:
            current_plugin.logger.error("Sign error: %s != %s", my_sign, sign)
            return None
        data = xmltodict.parse(raw_data)
        current_plugin.logger.info("Parsed data: %s", data)
        return data

    def _validate_sjtu_result(self, data):
        if data is not None:
            returncode = data["QueryResult"]["State"]["returncode"]
            if returncode != "0000":
                returnmsg = data["QueryResult"]["State"]["returnmsg"]
                current_plugin.logger.warn("Return code error: %s %s", returncode,
                                           returnmsg)
                return None
        return data


class RHSJTUSuccess(RHSJTUBase):
    """Confirmation message after successful payment"""

    def _process_args(self):
        self.sign = request.args['sign']
        self.raw_data = request.args['data']
        data = xmltodict.parse(self.raw_data)
        self.payment_result = data["payResult"]
        self.token = str(
            UUID(bytes=base64.urlsafe_b64decode(self.payment_result["billno"])))
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
        return redirect(url_for('event_registration.display_regform',
                                self.registration.locator.registrant))


class RHSJTUQuery(RHSJTUBase):
    def _process_args(self):
        self.sign = request.form['sign']
        self.raw_data = request.form['data']
        data = xmltodict.parse(self.raw_data)
        self.billinfo = data["billinfo"]
        self.token = str(UUID(bytes=base64.urlsafe_b64decode(self.billinfo["billno"])))
        self._init_registration(self.token)

    def _is_transaction_success_in_sjtu(self):
        query_url = f"{current_plugin.settings.get('url')}/payment/portal/Query_PayQuery.action"
        billno = self.billinfo["billno"]
        sign = self._generate_sign(billno)
        params = {
            "sign": sign,
            "sysid": self.sysid,
            "subsysid": self.subsysid,
            "billno": billno,
        }
        data = self._query_sjtu_portal(query_url, params)
        data = self._validate_sjtu_result(data)
        if data is None:
            return False
        if data["QueryResult"]["Billinfo"] is None:
            return False
        payment_results = data["QueryResult"]["Billinfo"]["billdetail"]
        if not isinstance(payment_results, list):
            payment_results = [payment_results]
        for payment_result in payment_results:
            if int(payment_result["paystate"]) == 4 and self._verify_amount(
                    float(payment_result["billamt"])):
                payment_result.pop("paystate")
                self._register_transaction(payment_result)
                return True
        return False

    def _process(self):
        current_plugin.logger.info("Query %s", self.billinfo["billno"])
        new_sign = self._generate_sign(self.raw_data)
        if new_sign != self.sign:
            current_plugin.logger.warn("Query %s: sign error", self.billinfo["billno"])
            current_plugin.logger.warn("Old sign: %s", self.sign)
            current_plugin.logger.warn("New sign: %s", new_sign)
            current_plugin.logger.warn("Raw data: %s", self.raw_data)
            return jsonify(success=False)
        elif self.registration.state != RegistrationState.unpaid:
            current_plugin.logger.info("Query %s: already paid in system",
                                       self.billinfo["billno"])
            return jsonify(success=False)
        elif self._is_transaction_success_in_sjtu():
            current_plugin.logger.info("Query %s: already paid in sjtu",
                                       self.billinfo["billno"])
            return jsonify(success=False)
        current_plugin.logger.info("Query %s: not paid", self.billinfo["billno"])
        return jsonify(success=True)


class RHSJTUInvoice(RHSJTUBase, RHRegistrationFormRegistrationBase):

    def _process_args(self):
        RHRegistrationFormRegistrationBase._process_args(self)
        self._init_plugin_settings()
        current_plugin.logger.info(self.registration)

    def _query_sjtu_tickets(self):
        query_url = f"{current_plugin.settings.get('url')}/payment_dzp/portal/TicketQuery.action"
        billno = uuid_to_billno(self.registration.uuid)
        sign = self._generate_sign(billno)
        params = {
            "sign": sign,
            "sysid": self.sysid,
            "subsysid": self.subsysid,
            "billno": billno,
        }
        data = self._query_sjtu_portal(query_url, params)
        data = self._validate_sjtu_result(data)
        if data is None:
            return []
        if data["QueryResult"]["Tickets"] is None:
            return []
        tickets = data["QueryResult"]["Tickets"]["tkinfo"]
        if not isinstance(tickets, list):
            tickets = [tickets]
        return tickets
        # ticket = {
        #     'type_no': "1002",
        #     'tk_typename': "中央非税收入统一票据_电子票",
        #     'taxtickettype': "00010118",
        #     'ticket_no': "0180906170950",
        #     'key': "7704tf",
        #     'feeitemdeford': "1169",
        #     'feeitemname': "本科生学费",
        #     'payamt': "90",
        # }
        # fake_data = [ticket, ticket]
        # return fake_data
        # return []

    def _process(self):
        sjtu_tickets = self._query_sjtu_tickets()
        return WPInvoice.render_template(
            'event_payment_invoice.html', self.event,
            regform=self.regform,
            # form_data=get_flat_section_submission_data(self.regform),
            # initial_values=initial_values,
            payment_conditions=payment_event_settings.get(self.event, 'conditions'),
            payment_enabled=self.event.has_feature('payment'),
            # invitation=self.invitation,
            registration=self.registration,
            management=False,
            login_required=self.regform.require_login and not session.user,
            is_restricted_access=self.is_restricted_access,
            sjtu_tickets=sjtu_tickets,
            # captcha_required=self._captcha_required,
            # captcha_settings=get_captcha_settings()
        )


class RHSJTUCallback(RHSJTUBase):

    def _process(self):
        return jsonify(success=True)

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


class RHSJTUSetRefund(RHSJTUBase, RHManageRegFormBase):

    def _process_args(self):
        RHManageRegFormBase._process_args(self)
        # self._init_plugin_settings()
        self.value = request.form["value"] == "true"
        self.registration = Registration.query.filter(
            Registration.registration_form_id == self.regform.id).one()

    def _process(self):
        # current_plugin.logger.info(self.registration)
        if self.registration.state != RegistrationState.complete:
            return jsonify(success=False)
        if not self.registration.transaction or self.registration.transaction.provider != "sjtu":
            return jsonify(success=False)
        if not self.registration.transaction.data:
            return jsonify(success=False)
        transaction_data = dict(self.registration.transaction.data)
        transaction_data['allow_refund'] = self.value
        self.registration.transaction.data = transaction_data
        current_plugin.logger.info("Allow refund: %s, %s", self.registration,
                                   self.registration.transaction.data)
        return jsonify(success=True)


class RHSJTURefund(RHSJTUBase, RHRegistrationFormRegistrationBase):

    def generate_refund_data(self):
        d = {
            "billno": uuid_to_billno(self.registration.uuid),
            "billamt": self.registration.transaction.data["billamt"],
            "feeitemid": current_plugin.event_settings.get(
                self.registration.registration_form.event, 'feeitemid'),
            "feeord": 1,
            "reason": "取消参加会议"
        }
        xml = dict2xml(d, wrap='refundBoll', indent="").replace("\n", "")
        return f"""<?xml version="1.0" encoding="GBK"?>{xml}"""

    def _process_GET(self):
        return jsonify_template('payment_sjtu:display/refund_transaction.html')

    def _process_POST(self):
        self._init_plugin_settings()
        query_url = f"{current_plugin.settings.get('url')}/payment/portal/appRefund.action"
        data = self.generate_refund_data()
        sign = self._generate_sign(data)
        params = {
            "sign": sign,
            "sysid": self.sysid,
            "subsysid": self.subsysid,
            "data": data,
        }
        data = self._query_sjtu_portal(query_url, params, unquote=True)
        redirect_url = url_for("event_registration.display_regform", self.registration.locator.registrant)
        base_error_msg = _("Refund failed, please contact an event manager. Reason: ")
        if data is None:
            flash(base_error_msg + _("API failed"), 'error')
            success = False
        elif data["refundResult"]["refundState"] == "1":
            flash(_("Refund successful."), 'info')
            success = True
        else:
            flash(base_error_msg + data["refundResult"]["errorMsg"], 'error')
            success = False
        return jsonify_data(flash=True, redirect=redirect_url, success=success)

# class RHSJTUCancel(RHSJTUIPN):
#     """Cancellation message"""
#
#     def _process(self):
#         flash(_('You cancelled the payment process.'), 'info')
#         return redirect(url_for('event_registration.display_regform', self.registration.locator.registrant))
