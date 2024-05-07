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
    IPV6 = 6

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
