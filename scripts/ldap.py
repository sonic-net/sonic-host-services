import ipaddress
import syslog

TLS1_2 = "SECURE128:SECURE192:SECURE256:-VERS-TLS1.0:-VERS-DTLS1.0:-VERS-TLS1.1:-SHA1"
TLS1_3 = "SECURE128:SECURE192:SECURE256:-VERS-TLS-ALL:-VERS-DTLS-ALL:+VERS-TLS1.3"


class LdapCfg:
    BASE = 'ou=users,dc=example,dc=com'
    BIND = ''
    BINDPW = ""
    VERSION = '3'
    TIMEOUT_SEARCH = 5
    TIMEOUT_BIND = 5
    PORT = 389
    SCOPE = "sub"
    HOST = ""
    HOSTNAME_CHECK = "no"
    GROUP_BASE_DN = "ou=users,dc=example,dc=com"
    GROUP_MEMBER_ATTR = "member"
    IPV6 = 6
    SSL_MODE = "none"
    CERT_VERIFY = "try"   # tls_reqcert never|allow|try|demand|hard
    # Folder for CA certs
    SSL_CACERT_FILE = "none"
    SSL_CIPHERS = "all"
    # CRL check is not implemented in current version of nslcd
    SSL_CRL_CHECK = "none"
    SSL_CRL_FILE = "default"

    @staticmethod
    def _do_cfg(_ldapsrvs_conf, attr, cfg_str):
        if _ldapsrvs_conf:
            attr = _ldapsrvs_conf[0].get(cfg_str, attr)
        return attr

    @staticmethod
    def cfg_base(_ldapsrvs_conf):
        return LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.BASE, 'base_dn')

    @staticmethod
    def cfg_bind(_ldapsrvs_conf):
        return LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.BIND, 'bind_dn')

    @staticmethod
    def cfg_bindpw(_ldapsrvs_conf):
        return LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.BINDPW, 'bind_password')

    @staticmethod
    def cfg_servers(_ldapsrvs_conf):
        servers_resp = LdapCfg.HOST
        if _ldapsrvs_conf:
            servers_resp = f"uri "
            ldap_mode = "ldap"
            port = LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.PORT, 'port')
            ssl_mode = LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.PORT, 'ssl_mode')
            if ssl_mode == 'ssl':
                port = LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.PORT, 'ssl_port')
                ldap_mode = "ldaps"
            for server in _ldapsrvs_conf:
                ip = server.get('ip', LdapCfg.HOST)
                try:
                    if ipaddress.ip_address(ip).version == LdapCfg.IPV6:
                        # LDAP require ipv6 to be in [], i.e uri ldap://[fdfd:fdfd:10:222:250:eeff:fe1b:56]/
                        ip = f"[{ip}]"
                        syslog.syslog(syslog.LOG_INFO, f"ldap server ip={ip} is an IPv6 address, "
                                      f"port={port}")
                    else:
                        syslog.syslog(syslog.LOG_INFO, f"ldap server ip={ip}, port={port}")
                except BaseException:
                    syslog.syslog(syslog.LOG_INFO, f"ldap server: {ip} its not a valid IP address, "
                                  f"maybe a domain name, port={port}")
                servers_resp += f"{ldap_mode}://{ip}:{port}/ "
            syslog.syslog(syslog.LOG_INFO, f"ldap servers list={servers_resp}")
        return servers_resp

    @staticmethod
    def cfg_version(_ldapsrvs_conf):
        return LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.VERSION, 'version')

    @staticmethod
    def cfg_scope(_ldapsrvs_conf):
        return LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.SCOPE, 'scope')

    @staticmethod
    def cfg_port(_ldapsrvs_conf):
        return LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.PORT, 'port')

    @staticmethod
    def cfg_timeout(_ldapsrvs_conf):
        return LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.TIMEOUT_SEARCH, 'search_timeout')

    @staticmethod
    def cfg_bind_timeout(_ldapsrvs_conf):
        return LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.TIMEOUT_BIND, 'bind_timeout')

    @staticmethod
    def cfg_ssl_mode(_ldapsrvs_conf):
        ssl_mode = LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.SSL_MODE, 'ssl_mode')
        ssl_modes = {
            "start-tls": "start_tls",
            "ssl": "on",
            "none": "off"
        }
        ret_ssl_mode = ssl_modes.get(ssl_mode, "")
        return ret_ssl_mode

    @staticmethod
    def cfg_tls_reqcert(_ldapsrvs_conf):
        ssl_mode = LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.SSL_MODE, 'ssl_mode')
        cert_verify_ret = ""
        # cert verify is only active in case ssl or start-tls are active
        if ssl_mode == 'ssl' or ssl_mode == 'start-tls':
            cert_verify_db = LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.CERT_VERIFY, 'cert_verify')
            if cert_verify_db == "enabled":
                cert_verify_ret = "tls_reqcert demand"
            elif cert_verify_db == "disabled":
                cert_verify_ret = "tls_reqcert never"
            else:
                syslog.syslog(syslog.LOG_WARNING, f"Cert verify contains an invalid value: {cert_verify_db}")
        else:
            cert_verify_ret = ""
        return cert_verify_ret

    @staticmethod
    def cfg_ca_certfile(_ldapsrvs_conf):
        default_ca_list = "/etc/ssl/certs/ca-certificates.crt"
        ca_list = LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.SSL_CACERT_FILE, 'ca_list')
        # Use default ca -list file. And add certificate manaement API to get list of certtificates
        if ca_list == 'none':
            ca_list_ret = ""
        else:
            ca_list_ret = f"tls_cacertfile {default_ca_list}"
        return ca_list_ret

    @staticmethod
    def cfg_tls_ciphers(_ldapsrvs_conf):
        tls_ciphers_ret = ""
        tls_ciphers = LdapCfg._do_cfg(_ldapsrvs_conf, LdapCfg.SSL_CIPHERS, 'tls_ciphers')
        if tls_ciphers == "all":
            tls_ciphers_ret = ""
        elif tls_ciphers == "TLS1.2":
            tls_ciphers_ret = f"tls_ciphers {TLS1_2}"
        elif tls_ciphers == "TLS1.3":
            tls_ciphers_ret = f"tls_ciphers {TLS1_3}"
        else:
            tls_ciphers_ret = ""
            syslog.syslog(syslog.LOG_ERR, f"LDAP TLS cipher contains an invalid value:: {tls_ciphers}")
        return tls_ciphers_ret
