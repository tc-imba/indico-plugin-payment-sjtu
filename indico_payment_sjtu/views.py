from indico.core.plugins import WPJinjaMixinPlugin
from indico.modules.events.registration.views import \
    WPDisplayRegistrationFormConference, WPManageRegistration
from indico.modules.events.views import WPConferenceDisplayBase


class WPInvoice(WPJinjaMixinPlugin, WPConferenceDisplayBase):
    menu_entry_name = 'registration'


class WPManageRegistrationSJTU(WPManageRegistration):
    template_prefix = ''


class WPDisplayRegistrationFormConferenceSJTU(WPDisplayRegistrationFormConference):
    template_prefix = ''
