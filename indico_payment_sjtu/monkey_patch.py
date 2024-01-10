"""
In this file, a few classes and functions in indico are monkey patched
to support some customized features.
The patched indico version is 3.2.3.
"""
from datetime import timedelta

from flask import redirect, session, flash
from indico.core import signals
from indico.core.config import config
from indico.core.db import db
from indico.core.notifications import make_email, send_email
from indico.modules.core.captcha import get_captcha_settings
from indico.modules.core.settings import core_settings
from indico.modules.designer import TemplateType
from indico.modules.designer.util import get_inherited_templates
from indico.modules.events.features.util import set_feature_enabled
from indico.modules.events.ical import MIMECalendar, event_to_ical
from indico.modules.events.models.events import EventType
from indico.modules.events.payment import payment_event_settings, payment_settings
from indico.modules.events.payment.models.transactions import TransactionStatus
from indico.modules.events.registration.controllers.management.regforms import RHManageParticipants, \
    _get_regform_creation_log_data, RHRegistrationFormCreate, RHRegistrationFormModify
from indico.modules.events.registration.forms import RegistrationFormCreateForm
from indico.modules.events.registration.lists import RegistrationListGenerator
from indico.modules.events.registration.controllers.display import RHRegistrationForm
from indico.modules.events.registration.controllers.management.reglists import \
    RHRegistrationsExportCSV, RHRegistrationsExportExcel, RHRegistrationsListManage
from indico.modules.events.registration import logger
from indico.modules.events.registration.models.form_fields import RegistrationFormFieldData, RegistrationFormField
from indico.modules.events.registration.models.forms import RegistrationForm
from indico.modules.events.registration.models.items import RegistrationFormItemType, RegistrationFormSection
from indico.modules.events.registration.models.registrations import RegistrationState, PublishRegistrationsMode, \
    RegistrationData
from indico.modules.events.registration.util import get_flat_section_submission_data, \
    get_initial_form_values, get_user_data, create_personal_data_fields, get_flat_section_setup_data
from indico.modules.events.registration.views import \
    WPDisplayRegistrationFormSimpleEvent, WPManageParticipants, WPManageRegistration
from indico.modules.logs import EventLogRealm, LogKind
from indico.modules.users.models.affiliations import Affiliation
from indico.util.date_time import format_date
from indico.util.decorators import strict_classproperty
from indico.util.enum import IndicoEnum
from indico.util.signals import make_interceptable, values_from_signal
from indico.util.spreadsheets import send_csv, send_xlsx, unique_col
from indico.util.string import camelize_keys
from indico.web.flask.templating import get_template_module
from indico.web.flask.util import url_for

from indico_payment_sjtu import _
from indico_payment_sjtu.util import uuid_to_billno
from indico_payment_sjtu.views import WPDisplayRegistrationFormConferenceSJTU, \
    WPManageRegistrationSJTU


def registration_list_generator_getattribute(self, item):
    result = super(RegistrationListGenerator, self).__getattribute__(item)
    if item == "static_items" and not hasattr(self, "monkey_patched"):
        self.monkey_patched = True
        result["billno"] = {
            'title': _('Bill Number'),
        }
        result["trade_no"] = {
            'title': _('Trade Number')
        }
    return result


def registration_list_generator_render_list(self):
    reg_list_kwargs = self.get_list_kwargs()
    tpl = get_template_module('payment_sjtu:management/_reglist.html')
    filtering_enabled = reg_list_kwargs.pop('filtering_enabled')
    return {
        'html': tpl.render_registration_list(**reg_list_kwargs),
        'filtering_enabled': filtering_enabled
    }


RegistrationListGenerator.__getattribute__ = registration_list_generator_getattribute
RegistrationListGenerator.render_list = registration_list_generator_render_list


def rh_registrations_list_manage_process(self):
    """List all registrations of a specific registration form of an event."""

    if self.list_generator.static_link_used:
        return redirect(self.list_generator.get_list_url())
    reg_list_kwargs = self.list_generator.get_list_kwargs()
    badge_templates = [tpl for tpl in
                       set(self.event.designer_templates) | get_inherited_templates(
                           self.event)
                       if tpl.type == TemplateType.badge]
    has_tickets = any(tpl.is_ticket for tpl in badge_templates)
    has_badges = any(not tpl.is_ticket for tpl in badge_templates)
    return WPManageRegistrationSJTU.render_template(
        'payment_sjtu:management/regform_reglist.html',
        self.event,
        has_badges=has_badges,
        has_tickets=has_tickets,
        **reg_list_kwargs)


RHRegistrationsListManage._process = rh_registrations_list_manage_process


def generate_spreadsheet_from_registrations(registrations, regform_items, static_items):
    """Generate a spreadsheet data from a given registration list.

    :param registrations: The list of registrations to include in the file
    :param regform_items: The registration form items to be used as columns
    :param static_items: Registration form information as extra columns
    """
    logger.info(static_items)
    field_names = ['ID', 'Name']
    special_item_mapping = {
        'reg_date': ('Registration date', lambda x: x.submitted_dt),
        'state': ('Registration state', lambda x: x.state.title),
        'price': ('Price', lambda x: x.render_price()),
        'checked_in': ('Checked in', lambda x: x.checked_in),
        'checked_in_date': (
            'Check-in date', lambda x: x.checked_in_dt if x.checked_in else ''),
        'payment_date': ('Payment date', lambda x: (x.transaction.timestamp
                                                    if (x.transaction is not None and
                                                        x.transaction.status == TransactionStatus.successful)
                                                    else '')),
        'tags_present': ('Tags', lambda x: [t.title for t in x.tags] if x.tags else ''),
        'billno': ('Bill Number', lambda x: uuid_to_billno(x.uuid)),
        'trade_no': ('Trade Number', lambda x: x.transaction.data['trade_no'] if (
                x.transaction is not None and x.transaction.provider == 'sjtu' and
                x.transaction.status == TransactionStatus.successful) else '')
    }
    for item in regform_items:
        field_names.append(unique_col(item.title, item.id))
        if item.input_type == 'accommodation':
            field_names.append(
                unique_col('{} ({})'.format(item.title, 'Arrival'), item.id))
            field_names.append(
                unique_col('{} ({})'.format(item.title, 'Departure'), item.id))
    field_names.extend(title for name, (title, fn) in special_item_mapping.items() if
                       name in static_items)
    rows = []
    for registration in registrations:
        data = registration.data_by_field
        registration_dict = {
            'ID': registration.friendly_id,
            'Name': f'{registration.first_name} {registration.last_name}'
        }
        for item in regform_items:
            key = unique_col(item.title, item.id)
            if item.input_type == 'accommodation':
                registration_dict[key] = data[item.id].friendly_data.get(
                    'choice') if item.id in data else ''
                key = unique_col('{} ({})'.format(item.title, 'Arrival'), item.id)
                arrival_date = data[item.id].friendly_data.get(
                    'arrival_date') if item.id in data else None
                registration_dict[key] = format_date(
                    arrival_date) if arrival_date else ''
                key = unique_col('{} ({})'.format(item.title, 'Departure'), item.id)
                departure_date = data[item.id].friendly_data.get(
                    'departure_date') if item.id in data else None
                registration_dict[key] = format_date(
                    departure_date) if departure_date else ''
            else:
                registration_dict[key] = data[
                    item.id].friendly_data if item.id in data else ''
        for name, (title, fn) in special_item_mapping.items():
            if name not in static_items:
                continue
            value = fn(registration)
            registration_dict[title] = value
        rows.append(registration_dict)
    return field_names, rows


# util.generate_spreadsheet_from_registrations = generate_spreadsheet_from_registrations

def rh_registrations_list_export_csv_process(self):
    headers, rows = generate_spreadsheet_from_registrations(
        self.registrations,
        self.export_config['regform_items'],
        self.export_config['static_item_ids']
    )
    return send_csv('registrations.csv', headers, rows)


RHRegistrationsExportCSV._process = rh_registrations_list_export_csv_process


def rh_registrations_list_export_xlsx_process(self):
    headers, rows = generate_spreadsheet_from_registrations(
        self.registrations,
        self.export_config['regform_items'],
        self.export_config['static_item_ids']
    )
    return send_xlsx('registrations.xlsx', headers, rows, tz=self.event.tzinfo)


RHRegistrationsExportExcel._process = rh_registrations_list_export_xlsx_process


def rh_registration_form_process_get(self):
    user_data = get_user_data(self.regform, session.user, self.invitation)
    initial_values = get_initial_form_values(self.regform) | user_data
    if self._captcha_required:
        initial_values |= {'captcha': None}

    if self.event.type_ == EventType.conference:
        view_class = WPDisplayRegistrationFormConferenceSJTU
        template = 'payment_sjtu:display/regform_display.html'
    else:
        view_class = WPDisplayRegistrationFormSimpleEvent
        template = 'display/regform_display.html'

    return view_class.render_template(
        template, self.event,
        regform=self.regform,
        form_data=get_flat_section_submission_data(self.regform),
        initial_values=initial_values,
        payment_conditions=payment_event_settings.get(self.event, 'conditions'),
        payment_enabled=self.event.has_feature('payment'),
        invitation=self.invitation,
        registration=self.registration,
        management=False,
        login_required=self.regform.require_login and not session.user,
        is_restricted_access=self.is_restricted_access,
        captcha_required=self._captcha_required,
        captcha_settings=get_captcha_settings())


RHRegistrationForm._process_GET = rh_registration_form_process_get


@make_interceptable
def _notify_registration(registration, template_name, to_managers=False, attach_rejection_reason=False,
                         diff=None, old_price=None):
    from indico.modules.events.registration.util import get_ticket_attachments
    attachments = []
    regform = registration.registration_form
    tickets_handled = values_from_signal(signals.event.is_ticketing_handled.send(regform), single_value=True)
    if (not to_managers and
            regform.tickets_enabled and
            regform.ticket_on_email and
            not any(tickets_handled) and
            registration.state == RegistrationState.complete):
        attachments += get_ticket_attachments(registration)
    if not to_managers and registration.registration_form.attach_ical:
        event_ical = event_to_ical(registration.event, method='REQUEST', skip_access_check=True,
                                   organizer=(core_settings.get('site_title'), config.NO_REPLY_EMAIL))
        attachments.append(MIMECalendar('event.ics', event_ical))
    to_list = (
        registration.email if not to_managers else registration.registration_form.manager_notification_recipients
    )
    from_address = registration.registration_form.notification_sender_address if not to_managers else None
    with registration.event.force_event_locale(registration.user if not to_managers else None):
        tpl = get_template_module(f'{template_name}', registration=registration,
                                  attach_rejection_reason=attach_rejection_reason, diff=diff, old_price=old_price)
        mail = make_email(to_list=to_list, template=tpl, html=True, from_address=from_address, attachments=attachments)
    user = session.user if session else None
    signals.core.before_notification_send.send('notify-registration', email=mail, registration=registration,
                                               template_name=template_name, to_managers=to_managers,
                                               attach_rejection_reason=attach_rejection_reason)
    send_email(mail, event=registration.registration_form.event, module='Registration', user=user,
               log_metadata={'registration_id': registration.id})


def notify_registration_creation(registration, notify_user=True):
    logger.info("notify_registration_creation")
    if notify_user:
        _notify_registration(registration, 'payment_sjtu:emails/registration_creation_to_registrant.html')
    if registration.registration_form.manager_notifications_enabled:
        _notify_registration(registration, 'events/registration/emails/registration_creation_to_managers.html',
                             to_managers=True)


def notify_registration_modification(registration, notify_user=True, diff=None, old_price=None):
    logger.info("notify_registration_modification")
    if notify_user:
        _notify_registration(registration, 'payment_sjtu:emails/registration_modification_to_registrant.html',
                             diff=diff, old_price=old_price)
    if registration.registration_form.manager_notifications_enabled:
        _notify_registration(registration, 'events/registration/emails/registration_modification_to_managers.html',
                             to_managers=True,
                             diff=diff, old_price=old_price)


def notify_registration_state_update(registration, attach_rejection_reason=False):
    logger.info("notify_registration_state_update")
    _notify_registration(registration, 'payment_sjtu:emails/registration_state_update_to_registrant.html',
                         attach_rejection_reason=attach_rejection_reason)
    if registration.registration_form.manager_notifications_enabled:
        _notify_registration(registration, 'events/registration/emails/registration_state_update_to_managers.html',
                             to_managers=True)


import indico.modules.events.payment.util

indico.modules.events.registration.util.notify_registration_creation = notify_registration_creation
indico.modules.events.registration.util.notify_registration_modification = notify_registration_modification
indico.modules.events.registration.controllers.display.notify_registration_state_update = notify_registration_state_update
indico.modules.events.registration.controllers.management.reglists.notify_registration_state_update = notify_registration_state_update
indico.modules.events.payment.util.notify_registration_state_update = notify_registration_state_update


class InvoiceDataType(int, IndicoEnum):
    """
    Description of the invoice data items that exist on every registration form.
    """

    __titles__ = [
        None,
        '普通增值税发票需求',
        '付款单位名称',
        '统一社会信用代码',
        '手机号',
    ]
    __description__ = [
        None,
        'The receipt is only valid for Chinese Mainland. For receipt / invoice outside of China, you will be automatically obtained in the email received after registration is completed.',
        'Input the receipt title (the name of your affiliation). 填写发票付款单位名称。',
        'For enterprises and institutions in Chinese mainland, it is mandatory to fill in the Unified Social Credit Code Taxpayer Identification Number. 如果是中国大陆的企事业单位，则必须填写统一社会信用代码。',
        'The mobile phone number used to receive receipt information. Limited to 11-digit mobile phone number in Mainland China. 用于接受发票信息的手机号，限中国大陆11位手机号。',
    ]
    receipt = 1
    receipt_title = 2
    receipt_number = 3
    receipt_phone = 4

    def get_title(self):
        return self.__titles__[self]

    def get_description(self):
        return self.__description__[self]

    @strict_classproperty
    @classmethod
    def FIELD_DATA(cls):
        return [
            (cls.receipt, {
                'title': cls.receipt.get_title(),
                'description': cls.receipt.get_description(),
                'input_type': 'bool',
                'position': 1
            }),
            (cls.receipt_title, {
                'title': cls.receipt_title.get_title(),
                'description': cls.receipt_title.get_description(),
                'input_type': 'text',
                'position': 2
            }),
            (cls.receipt_number, {
                'title': cls.receipt_number.get_title(),
                'description': cls.receipt_number.get_description(),
                'input_type': 'text',
                'position': 3
            }),
            (cls.receipt_phone, {
                'title': cls.receipt_phone.get_title(),
                'description': cls.receipt_phone.get_description(),
                'input_type': 'text',
                'position': 4
            }),
        ]

    @property
    def is_required(self):
        return self in {InvoiceDataType.receipt}

    @property
    def column(self):
        """
        The Registration column in which the value is stored in
        addition to the regular registration data entry.
        """
        if self in {
            InvoiceDataType.receipt,
            InvoiceDataType.receipt_title,
            InvoiceDataType.receipt_number,
            InvoiceDataType.receipt_phone,
        }:
            return self.name
        else:
            return None


def create_invoice_data_fields(regform: RegistrationForm):
    """Create the special section/fields for invoice data."""
    title = 'Receipt Payer Data'
    description = '(Only valid for Chinese Mainland) 普通增值税发票付款人信息'
    section = next(
        (s for s in regform.sections if s.type == RegistrationFormItemType.section and s.title == title), None)
    if section is None:
        section = RegistrationFormSection(registration_form=regform, title=title, description=description)
        missing = set(InvoiceDataType)
    else:
        existing = {x.type for x in section.children if x.type == RegistrationFormItemType.field}
        missing = set(InvoiceDataType) - existing
    for pd_type, data in InvoiceDataType.FIELD_DATA:
        if pd_type not in missing:
            continue
        field = RegistrationFormField(registration_form=regform, parent_id=section.id,
                                      type=RegistrationFormItemType.field, is_required=pd_type.is_required)
        for key, value in data.items():
            setattr(field, key, value)
        field.data, versioned_data = field.field_impl.process_field_data(data.pop('data', {}))
        field.current_data = RegistrationFormFieldData(versioned_data=versioned_data)
        section.children.append(field)


def rh_manage_participants_process(self):
    regform = self.event.participation_regform
    registration_enabled = self.event.has_feature('registration')
    participant_visibility = (PublishRegistrationsMode.show_with_consent
                              if self.event.type_ == EventType.lecture
                              else PublishRegistrationsMode.show_all)
    public_visibility = (PublishRegistrationsMode.show_with_consent
                         if self.event.type_ == EventType.lecture
                         else PublishRegistrationsMode.show_all)
    form = RegistrationFormCreateForm(title='Participants',
                                      visibility=[participant_visibility.name, public_visibility.name, None])
    if form.validate_on_submit():
        set_feature_enabled(self.event, 'registration', True)
        if not regform:
            regform = RegistrationForm(event=self.event, is_participation=True,
                                       currency=payment_settings.get('currency'))
            create_personal_data_fields(regform)
            create_invoice_data_fields(regform)
            form.populate_obj(regform, skip=['visibility'])
            participant_visibility, public_visibility, visibility_duration = form.visibility.data
            regform.publish_registrations_participants = PublishRegistrationsMode[participant_visibility]
            regform.publish_registrations_public = PublishRegistrationsMode[public_visibility]
            regform.publish_registrations_duration = (timedelta(weeks=visibility_duration)
                                                      if visibility_duration is not None else None)
            db.session.add(regform)
            db.session.flush()
            signals.event.registration_form_created.send(regform)
            regform.log(EventLogRealm.management, LogKind.positive, 'Registration',
                        f'Registration form "{regform.title}" has been created', session.user,
                        data=_get_regform_creation_log_data(regform))
        return redirect(url_for('event_registration.manage_regform', regform))

    if not regform or not registration_enabled:
        return WPManageParticipants.render_template('management/participants.html', self.event, form=form,
                                                    regform=regform, registration_enabled=registration_enabled)
    return redirect(url_for('event_registration.manage_regform', regform))


RHManageParticipants._process = rh_manage_participants_process


def rh_registration_form_create_process(self):
    participant_visibility = (PublishRegistrationsMode.hide_all
                              if self.event.type_ == EventType.conference
                              else PublishRegistrationsMode.show_all)
    public_visibility = PublishRegistrationsMode.hide_all
    form = RegistrationFormCreateForm(event=self.event,
                                      visibility=[participant_visibility.name, public_visibility.name, None])
    if form.validate_on_submit():
        regform = RegistrationForm(event=self.event, currency=payment_settings.get('currency'))
        create_personal_data_fields(regform)
        create_invoice_data_fields(regform)
        form.populate_obj(regform, skip=['visibility'])
        participant_visibility, public_visibility, visibility_duration = form.visibility.data
        regform.publish_registrations_participants = PublishRegistrationsMode[participant_visibility]
        regform.publish_registrations_public = PublishRegistrationsMode[public_visibility]
        regform.publish_registrations_duration = (timedelta(weeks=visibility_duration)
                                                  if visibility_duration is not None else None)
        db.session.add(regform)
        db.session.flush()
        signals.event.registration_form_created.send(regform)
        flash(_('Registration form has been successfully created'), 'success')
        regform.log(EventLogRealm.management, LogKind.positive, 'Registration',
                    f'Registration form "{regform.title}" has been created', session.user,
                    data=_get_regform_creation_log_data(regform))
        return redirect(url_for('.manage_regform', regform))
    return WPManageRegistration.render_template('management/regform_create.html', self.event,
                                                form=form, regform=None)


RHRegistrationFormCreate._process = rh_registration_form_create_process


def rh_registration_form_modify_process(self):
    form_data = get_flat_section_setup_data(self.regform)
    # this is a bit dirty, but we can't find the exact section with anything else than title
    receipt_section_id = None
    for section_id in form_data['sections'].keys():
        if "Receipt" in form_data['sections'][section_id]['title']:
            receipt_section_id = section_id
            # form_data['sections'][section_id]['isPersonalData'] = True
            break
    if receipt_section_id is not None:
        for item_id in form_data['items'].keys():
            if form_data['items'][item_id]['sectionId'] == receipt_section_id:
                form_data['items'][item_id]['fieldIsPersonalData'] = True  # remove delete button
                # if form_data['items'][item_id]['inputType'] == 'bool':
                form_data['items'][item_id]['fieldIsRequired'] = True  # remove hide button and disable edit "required"

    return WPManageRegistration.render_template('management/regform_modify.html', self.event,
                                                form_data=form_data,
                                                regform=self.regform,
                                                has_predefined_affiliations=Affiliation.query.has_rows())


RHRegistrationFormModify._process = rh_registration_form_modify_process
