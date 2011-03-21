from setuptools import setup, find_packages

setup(name='neo',
    version='0.1.0',
    description='Distributed, redundant and transactional storage for ZODB- Common part',
    author='NEOPPOD',
    author_email='neo-dev@erp5.org',
    url='http://www.neoppod.org/',
    license="GPL 2",

    packages=['neo.lib'],
    package_dir={
            'neo':'neo',
    },
    namespace_packages=['neo'],

    package_data = {
        'neo': [
            'component.xml',
        ],
    },
    zip_safe=False,
)
