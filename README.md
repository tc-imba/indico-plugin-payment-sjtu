# SJTU Payment Plugin

This plugin provides a SJTU payment option for Indico's payment module.

When used, the user will be sent to SJTU payment platform to make the payment, and afterward
they are automatically sent back to Indico. 

This plugin is based on https://github.com/indico/indico-plugins/tree/master/payment_paypal.

[//]: # (It relies on PayPal's IPN payment)

[//]: # (notification for Indico to automatically mark the registrant as paid once the)

[//]: # (payment has been made and processed by PayPal.)

## Installation

The translations of the plugin are automatically compiled when installing.

```bash
# using the indico python environment
pip install -e .
```

## Development

The translations (in the `message.po` file) of the plugin should be updated after any code change. 

```bash
pybabel extract -o indico_payment_sjtu/translations/messages.pot indico_payment_sjtu -F babel.cfg
pybabel update -i indico_payment_sjtu/translations/messages.pot -l zh_Hans_CN -d indico_payment_sjtu/translations
pybabel compile -d indico_payment_sjtu/translations/
```

The local development environment can be deployed with docker.

```bash
cd deploy
cp indico.env.template indico.env
docker compose up --build -d
```