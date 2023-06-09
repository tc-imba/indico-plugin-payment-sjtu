from indico.core.plugins import WPJinjaMixinPlugin
from indico.modules.events.views import WPConferenceDisplayBase


class WPInvoice(WPJinjaMixinPlugin, WPConferenceDisplayBase):
    menu_entry_name = 'invoice'
