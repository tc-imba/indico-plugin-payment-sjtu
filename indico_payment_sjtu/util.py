# This file is part of the Indico plugins.
# Copyright (C) 2002 - 2023 CERN
#
# The Indico plugins are free software; you can redistribute
# them and/or modify them under the terms of the MIT License;
# see the LICENSE file for more details.

import re

from wtforms import ValidationError

from indico.util.string import is_valid_mail

from indico_payment_sjtu import _


def validate_business(form, field):
    """Validates a PayPal business string.

    It can either be an email address or a paypal business account ID.
    """
    if not is_valid_mail(field.data, multi=False) and not re.match(r'^[a-zA-Z0-9]{13}$', field.data):
        raise ValidationError(_('Invalid email address / paypal ID'))
