from setuptools import setup
import subprocess

def setup_fake(**kwargs):
    install_list = ['requires', 'tests_require', 'install_requires' ]
    for keyword in install_list:
        packages = kwargs.get(keyword)
        if packages:
            for package in packages:
                r = subprocess.call([sys.executable, '-m', 'pip', 'show', package.split("==")[0]], stdout=sys.stderr.fileno())
                if r != 0:
                    print("\033[33mPlease build and install SONiC python wheels dependencies from github.com/sonic-net/sonic-buildimage\033[0m", file=sys.stderr)
                    print("\033[33mThen install other dependencies from Pypi\033[0m", file=sys.stderr)
                    exit(1)
    setup(**kwargs)

setup_fake(
    name = 'sonic-host-services',
    version = '1.0',
    description = 'Python services which run in the SONiC host OS',
    license = 'Apache 2.0',
    author = 'SONiC Team',
    author_email = 'linuxnetdev@microsoft.com',
    url = 'https://github.com/Azure/sonic-buildimage',
    maintainer = 'Joe LeVeque',
    maintainer_email = 'jolevequ@microsoft.com',
    packages = [
        'host_modules'
    ],
    scripts = [
        'scripts/caclmgrd',
        'scripts/hostcfgd',
        'scripts/aaastatsd',
        'scripts/procdockerstatsd',
        'scripts/determine-reboot-cause',
        'scripts/process-reboot-cause',
        'scripts/sonic-host-server'
    ],
    install_requires = [
        'dbus-python',
        'systemd-python',
        'Jinja2>=2.10',
        'PyGObject',
        'sonic-py-common'
    ],
    setup_requires = [
        'pytest-runner',
        'wheel'
    ],
    tests_require = [
        'parameterized',
        'pytest',
        'pyfakefs',
        'sonic-py-common',
        'deepdiff==6.2.2'
    ],
    classifiers = [
        'Development Status :: 3 - Alpha',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'Intended Audience :: Information Technology',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: Apache Software License',
        'Natural Language :: English',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3.7',
        'Topic :: System',
    ],
    keywords = 'sonic SONiC host services',
    test_suite = 'setup.get_test_suite'
)
