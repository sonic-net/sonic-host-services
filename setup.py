from __future__ import print_function
import sys
from setuptools import setup
import pkg_resources
from packaging import version

# sonic_dependencies, version requirement only supports '>='
sonic_dependencies = ['sonic-py-common', 'sonic-utilities']
testing_dependencies = [
    'parameterized',
    'pytest',
    'pytest-cov',
    'pyfakefs',
    'deepdiff>=6.2.2',
]
for package in sonic_dependencies:
    try:
        package_dist = pkg_resources.get_distribution(package.split(">=")[0])
    except pkg_resources.DistributionNotFound:
        print(package + " is not found!", file=sys.stderr)
        print("Please build and install SONiC python wheels dependencies from sonic-buildimage", file=sys.stderr)
        exit(1)
    if ">=" in package:
        if version.parse(package_dist.version) >= version.parse(package.split(">=")[1]):
            continue
        print(package + " version not match!", file=sys.stderr)
        exit(1)

setup(
    name = 'sonic-host-services',
    version = '1.0',
    description = 'Python services which run in the SONiC host OS',
    python_requires = '>=3.9',
    license = 'Apache 2.0',
    author = 'SONiC Team',
    author_email = 'linuxnetdev@microsoft.com',
    url = 'https://github.com/Azure/sonic-buildimage',
    maintainer = 'Joe LeVeque',
    maintainer_email = 'jolevequ@microsoft.com',
    packages = [
        'dldd',
        'dldd.rule_schema',
        'host_modules',
        'utils'
    ],
    # Map packages to their actual dirs
    package_dir = {
        'dldd': 'dldd',
        'host_modules': 'host_modules',
        'utils': 'utils'
    },
    package_data = {
        'dldd': ['schemas/*.json']
    },
    scripts=[
        'scripts/caclmgrd',
        'scripts/hostcfgd',
        'scripts/featured',
        'scripts/aaastatsd',
        'scripts/procdockerstatsd',
        'scripts/determine-reboot-cause',
        'scripts/process-reboot-cause',
        'scripts/gnoi_shutdown_daemon.py',
        'scripts/sonic-host-server',
        'scripts/ldap.py',
        'scripts/console-monitor',
        'scripts/dldd',
        'scripts/dldd-rules-watch'
    ],
    install_requires = [
        'dbus-python',
        'systemd-python',
        'Jinja2>=2.10',
        'PyGObject',
        'pycairo==1.26.1',
        'psutil',
        'PyYAML',
        'redis>=3.5.3',
        'pydantic==2.13.4',
        'regex==2024.11.6',
    ] + sonic_dependencies,
    setup_requires = [
        'pytest-runner',
        'wheel'
    ],
    tests_require = testing_dependencies,
    extras_require = {
        "testing": testing_dependencies
    },
    classifiers = [
        'Development Status :: 3 - Alpha',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'Intended Audience :: Information Technology',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: Apache Software License',
        'Natural Language :: English',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3',
        'Topic :: System',
    ],
    keywords = 'sonic SONiC host services',
    test_suite = 'setup.get_test_suite'
)
