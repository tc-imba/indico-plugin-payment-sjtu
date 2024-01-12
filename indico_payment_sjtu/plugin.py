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

from flask_pluginengine import current_plugin, render_plugin_template
from flask import session
from indico.modules.events.layout.util import MenuEntryData
from wtforms.fields import StringField, URLField
from wtforms.validators import DataRequired, Optional

from indico.core.plugins import IndicoPlugin, url_for_plugin
from indico.core import signals
from indico.modules.events.payment import (PaymentEventSettingsFormBase, PaymentPluginMixin,
                                           PaymentPluginSettingsFormBase)
from indico.util.string import remove_accents, str_to_ascii
from indico.web.forms.validators import UsedIf

from indico_payment_sjtu import _
from indico_payment_sjtu.blueprint import blueprint
from indico_payment_sjtu.util import uuid_to_billno


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
                        'url': 'https://www.jdcw.sjtu.edu.cn',
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
    def get_registration_form_fields(data):
        registration_form_fields = {}
        for section in data["registration"].registration_form.sections:
            for field in section.fields:
                # current_plugin.logger.info(field.__dict__)
                registration_form_fields[field.current_data_id] = field.title
        registration_form_data = {}
        for registration_data in data["registration"].data:
            # current_plugin.logger.info(registration_data.__dict__)
            current_data_id = registration_data.field_data_id
            if current_data_id in registration_form_fields:
                registration_form_data[registration_form_fields[current_data_id]] = registration_data.data
        current_plugin.logger.info(registration_form_data)
        return registration_form_data

    @staticmethod
    def generate_invoice_data(data):
        registration_form_data = SJTUPaymentPlugin.get_registration_form_fields(data)
        zz_unit = registration_form_data.get("付款单位全称", "")
        if zz_unit:
            tax_code = registration_form_data.get("统一社会信用代码（税号）", "")
            zz_mobile = registration_form_data.get("手机号", "")
            zz_email = data["registration"].email
            type_no = "3001"
        else:
            tax_code = ""
            zz_mobile = ""
            zz_email = ""
            type_no = ""
        return zz_unit, tax_code, zz_mobile, zz_email, type_no

    @staticmethod
    def generate_payment_data(data):
        # for section in data["registration"].registration_form.sections:
        #     current_plugin.logger.info(section.fields)

        zz_unit, tax_code, zz_mobile, zz_email,  type_no = SJTUPaymentPlugin.generate_invoice_data(data)

        d = {
            # "version": "1.0.0.5",
            "billno": data["billno"],
            "orderinfono": "...",
            "orderinfoname": f'{data["registration"].first_name} {data["registration"].last_name}',
            "returnURL": data['return_url'],
            "billremark": f'会议名称：{data["event"].title}，参会人员：{data["registration"].first_name} {data["registration"].last_name}',
            "tax_code": tax_code,
            # "zz_address": "",
            # "zz_bank": "",
            # "zz_bankname": "",
            # "zz_tel": "",
            "zz_unit": zz_unit,
            "zz_mobile": zz_mobile,
            "zz_email": zz_email,
            "type_no": type_no,
            # "paystyle": "",
            "billdtl": {
                "feeitemid": data["event_settings"]["feeitemid"],
                "feeord": 1,
                "amt": data["amount"],
                "dtlremark": "",
                "unit": "项",
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
        data["billno"] = uuid_to_billno(data["registration"].locator.uuid["token"])
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

# @signals.event.sidemenu.connect
# def _extend_event_menu(sender, **kwargs):
#     from indico.modules.events.registration.models.forms import RegistrationForm
#     from indico.modules.events.registration.models.registrations import Registration
#     #
#     def _visible_registration(event):
#         if not event.has_feature('registration'):
#             return False
#         if not event.can_access(session.user) and not (event.has_regform_in_acl and event.public_regform_access):
#             return False
#         if any(form.is_scheduled for form in event.registration_forms):
#             return True
#         if not session.user:
#             return False
#         return (Registration.query.with_parent(event)
#                 .join(Registration.registration_form)
#                 .filter(Registration.user == session.user,
#                         ~RegistrationForm.is_deleted)
#                 .has_rows())
#
#     yield MenuEntryData(_('Invoice'), 'invoice', 'plugin_payment_sjtu.invoices', position=12,
#                         visible=_visible_registration, hide_if_restricted=False)
