# This file is part of the Indico plugins.
# Copyright (C) 2002 - 2023 CERN
#
# The Indico plugins are free software; you can redistribute
# them and/or modify them under the terms of the MIT License;
# see the LICENSE file for more details.

from hashlib import md5
import base64
from dict2xml import dict2xml
from urllib.parse import urlparse, urljoin
from uuid import UUID

from flask_pluginengine import render_plugin_template
from wtforms.fields import StringField, URLField
from wtforms.validators import DataRequired, Optional

from indico.core.plugins import IndicoPlugin, url_for_plugin
from indico.modules.events.payment import (PaymentEventSettingsFormBase, PaymentPluginMixin,
                                           PaymentPluginSettingsFormBase)
from indico.util.string import remove_accents, str_to_ascii
from indico.web.forms.validators import UsedIf

from indico_payment_sjtu import _
from indico_payment_sjtu.blueprint import blueprint
from indico_payment_sjtu.util import validate_business


class PluginSettingsForm(PaymentPluginSettingsFormBase):
    url = URLField(_('API URL'), [DataRequired()], description=_('URL of the SJTU HTTP API.'))
    cert = StringField(_('Cert'), [DataRequired()], description=_('The cert obtained from SJTU Pay.'))
    sysid = StringField(_('Sys Id'), [Optional()],
                        description=_('The sysid parameter of SJTU Pay.'))
    subsysid = StringField(_('Sub Sys Id'), [Optional()],
                           description=_('The subsysid parameter of SJTU Pay.'))
    feeitemid = StringField(_('Fee Item Id'), [Optional()],
                            description=_('The feeitemid from SJTU Pay.'))


class EventSettingsForm(PaymentEventSettingsFormBase):
    sysid = StringField(_('Sys Id'), [UsedIf(lambda form, _: form.enabled.data), DataRequired()],
                        description=_('The sysid parameter of SJTU Pay.'))
    subsysid = StringField(_('Sub Sys Id'), [UsedIf(lambda form, _: form.enabled.data), DataRequired()],
                           description=_('The subsysid parameter of SJTU Pay.'))
    feeitemid = StringField(_('Fee Item Id'), [UsedIf(lambda form, _: form.enabled.data), DataRequired()],
                            description=_('The feeitemid from SJTU Pay.'))


class SJTUPaymentPlugin(PaymentPluginMixin, IndicoPlugin):
    """SJTU Pay

    Provides a payment method using the SJTU IPN API.
    """
    configurable = True
    settings_form = PluginSettingsForm
    event_settings_form = EventSettingsForm
    default_settings = {'method_name': 'SJTU Pay',
                        'url': 'https://www.jdcw.sjtu.edu.cn/payment',
                        'cert': '',
                        'sysid': '',
                        'subsysid': '',
                        'feeitemid': ''}
    default_event_settings = {'enabled': False,
                              'method_name': None,
                              'sysid': None,
                              'subsysid': None,
                              'feeitemid': None
                              }

    def init(self):
        super().init()
        # self.template_hook('event-manage-payment-plugin-before-form', self._get_encoding_warning)

    @property
    def logo_url(self):
        return url_for_plugin(self.name + '.static', filename='images/logo.png')

    def get_blueprints(self):
        return blueprint

    @staticmethod
    def generate_url(data, endpoint):
        url_with_query = url_for_plugin(endpoint, data["registration"].locator.uuid, _external=True)
        # remove query parameter with some magic
        return urljoin(url_with_query, urlparse(url_with_query).path)

    @staticmethod
    def generate_billno(data):
        # we use base64 encode here because sjtu payment only supports billno <= 30 characters,
        # but uuid is longer than the limit
        token = UUID(data["registration"].locator.uuid["token"])
        b64_token = base64.b64encode(token.bytes).decode("ascii")
        return b64_token

    @staticmethod
    def generate_payment_data(data):
        d = {
            # "version": "1.0.0.5",
            "billno": data["billno"],
            "orderinfono": "...",
            "orderinfoname": f'{data["registration"].first_name} {data["registration"].last_name}',
            "returnURL": data['return_url'],
            # "billremark": "",
            # "tax_code": "",
            # "zz_address": "",
            # "zz_bank": "",
            # "zz_bankname": "",
            # "zz_tel": "",
            # "zz_unit": "",
            # "zz_mobile": "",
            # "type_no": "",
            # "paystyle": "",
            "billdtl": {
                "feeitemid": data["event_settings"]["feeitemid"],
                "feeord": 1,
                "amt": data["amount"],
                "dtlremark": ""
            }
        }
        xml = dict2xml(d, wrap='billinfo', indent="").replace("\n", "")
        return f"""<?xml version="1.0" encoding="GBK"?>{xml}"""

    @staticmethod
    def generate_sign(data, body):
        sysid = data["event_settings"]["sysid"]
        subsysid = data["event_settings"]["subsysid"]
        cert = data["settings"]["cert"]
        # print(sysid, subsysid, cert, body)
        md5_string = sysid + subsysid + cert + body
        return md5(md5_string.encode("utf-8")).hexdigest()

    def adjust_payment_form_data(self, data):
        event = data['event']
        registration = data['registration']
        data['item_name'] = '{}: registration for {}'.format(
            str_to_ascii(remove_accents(registration.full_name)),
            str_to_ascii(remove_accents(event.title))
        )
        data["billno"] = self.generate_billno(data)
        data['return_url'] = self.generate_url(data, "payment_sjtu.success")
        data['query_url'] = self.generate_url(data, "payment_sjtu.query")
        # data['cancel_url'] = url_for_plugin('payment_sjtu.cancel', _external=True)
        # data['notify_url'] = url_for_plugin('payment_sjtu.notify', _external=True)
        data['payment_data'] = self.generate_payment_data(data)
        data['payment_sign'] = self.generate_sign(data, data['payment_data'])
        data['query_sign'] = self.generate_sign(data, data['billno'])
        data['payment_data_base64'] = base64.b64encode(data['payment_data'].encode("utf-8")).decode("ascii")

    def _get_encoding_warning(self, plugin=None, event=None):
        if plugin == self:
            return render_plugin_template('event_settings_encoding_warning.html')
