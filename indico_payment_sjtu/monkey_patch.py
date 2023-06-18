from flask import redirect

from indico.modules.designer import TemplateType
from indico.modules.designer.util import get_inherited_templates
from indico.modules.events.payment.models.transactions import TransactionStatus
from indico.modules.events.registration.lists import RegistrationListGenerator
from indico.modules.events.registration.controllers.management.reglists import \
    RHRegistrationsExportCSV, RHRegistrationsExportExcel, RHRegistrationsListManage
from indico.modules.events.registration import logger
from indico.modules.events.registration import util
from indico.util.date_time import format_date
from indico.util.spreadsheets import send_csv, send_xlsx, unique_col

from indico_payment_sjtu import _
from indico_payment_sjtu.util import uuid_to_billno
from indico_payment_sjtu.views import WPManageRegistrationSJTU


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


RegistrationListGenerator.__getattribute__ = registration_list_generator_getattribute


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
