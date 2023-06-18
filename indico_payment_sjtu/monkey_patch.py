from flask import redirect

from indico.modules.designer import TemplateType
from indico.modules.designer.util import get_inherited_templates
from indico.modules.events.registration.lists import RegistrationListGenerator
from indico.modules.events.registration.controllers.management.reglists import \
    RHRegistrationsListManage
from indico.modules.events.registration import logger

from indico_payment_sjtu import _
from indico_payment_sjtu.views import WPManageRegistrationSJTU


def registration_list_generator_getattribute(self, item):
    result = super(RegistrationListGenerator, self).__getattribute__(item)
    if item == "static_items" and not hasattr(self, "monkey_patched"):
        self.monkey_patched = True
        result["billno"] = {
            'title': _('Bill No.'),
        }
        result["trade_no"] = {
            'title': _('Trade No.')
        }
    return result


RegistrationListGenerator.__getattribute__ = registration_list_generator_getattribute


def rh_registrations_list_manage_process(self):
    """List all registrations of a specific registration form of an event."""
    logger.info("test")

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

