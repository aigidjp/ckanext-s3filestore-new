# -*- coding: utf-8 -*-

pytest_plugins = [
    u'ckanext.s3filestore.tests.fixtures',
    # 'ckan.tests.pytest_ckan.ckan_setup',
    # u'ckan.tests.pytest_ckan.fixtures',
]


def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'ckan_config_dynamic(key, value): override CKAN config values, '
        'including options declared via config_declaration.yaml'
    )
