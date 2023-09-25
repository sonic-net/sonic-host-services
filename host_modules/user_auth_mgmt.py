""" Host User Authentication management dbus endpoint handler"""
import host_service
import pwd, grp, syslog

mod_name= 'user_auth_mgmt'

class UserAuthMgmt(host_service.HostModule):
    """DBus endpoint that handles Infra user authentication related operations """


    def __init__(self,  name):
        super().__init__(name)

    @staticmethod
    def get_user_roles(username):
        """ Return the user role to the provided username"""
        output = ","
        roles = []
        try:
            pwd.getpwnam(username)
        except:
            syslog.syslog(syslog.LOG_ERR,"Invalid user")
            return 1,"Invalid user"
        gids = [g.gr_gid for g in grp.getgrall() if username in g.gr_mem]
        gid = pwd.getpwnam(username).pw_gid
        gids.append(grp.getgrgid(gid).gr_gid)
        roles = [grp.getgrgid(gid).gr_name for gid in gids]
        if len(roles) > 0:
            output = output.join(roles)
        else:
            return 1,"No roles for the user"
        return 0,output

    @host_service.method(host_service.bus_name(mod_name), in_signature='s', out_signature='is')
    def retrieve_user_roles(self, options):
        return UserAuthMgmt.get_user_roles(options)


def register():
    """Return class name"""
    return UserAuthMgmt, mod_name
