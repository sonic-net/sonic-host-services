#include <iostream>
#include <string>
#include <vector>
#include <map>
#include <set>
#include <memory>
#include <thread>
#include <chrono>
#include <fstream>
#include <sstream>
#include <iomanip>
#include <algorithm>
#include <regex>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <unistd.h>
#include <pwd.h>
#include <grp.h>
#include <shadow.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <spawn.h>
#include <syslog.h>
#include <nlohmann/json.hpp>

extern char **environ;

#include <swss/dbconnector.h>
#include <swss/configdb.h>
#include <swss/table.h>
#include <swss/subscriberstatetable.h>
#include <swss/select.h>
#include <swss/logger.h>
#include <nlohmann/json.hpp>

// Constants
const std::string LOCAL_USER_TABLE = "LOCAL_USER";
const std::string LOCAL_ROLE_SECURITY_POLICY_TABLE = "LOCAL_ROLE_SECURITY_POLICY";
const std::string DEVICE_METADATA_TABLE = "DEVICE_METADATA";
const std::string DEVICE_METADATA_LOCALHOST_KEY = "localhost";
const std::string LOCAL_USER_MANAGEMENT_FIELD = "local_user_management";

const std::string PAM_FAILLOCK_CONF = "/etc/security/faillock.conf";
const std::string PAM_FAILLOCK_TEMPLATE = "/usr/share/sonic/templates/faillock.conf.j2";

// Group for tracking users managed by userd
const std::string MANAGED_USER_GROUP = "local_mgd";

// System users to exclude from management
const std::set<std::string> SYSTEM_USERS = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "gnats",
    "nobody", "_apt", "systemd-network", "systemd-resolve", "messagebus",
    "systemd-timesync", "sshd", "redis", "ntp", "frr", "snmp"
};

// Role to group mappings
const std::map<std::string, std::vector<std::string>> ROLE_GROUPS = {
    {"administrator", {"sudo", "docker", "redis", "admin"}},
    {"operator", {"users"}}
};

// UID range for managed users
const uid_t MIN_USER_UID = 1000;
const uid_t MAX_USER_UID = 60000;

// Global variables for signal handling
volatile sig_atomic_t g_shutdown = 0;

void signal_handler(int sig) {
    switch (sig) {
        case SIGHUP:
            SWSS_LOG_INFO("userd: signal SIGHUP caught and ignoring...");
            break;
        case SIGINT:
        case SIGTERM:
            SWSS_LOG_INFO("userd: signal %s caught, shutting down...",
                   sig == SIGINT ? "SIGINT" : "SIGTERM");
            g_shutdown = 1;
            break;
        default:
            SWSS_LOG_INFO("userd: invalid signal %d - ignoring...", sig);
            break;
    }
}

class SystemCommand {
public:
    static bool execute(const std::vector<std::string>& cmd, const std::set<int>& mask_args = {}) {
        if (cmd.empty()) {
            return false;
        }

        // Build command string for logging with sensitive arguments masked
        std::string command_str;
        for (size_t i = 0; i < cmd.size(); ++i) {
            if (i > 0) command_str += " ";
            if (mask_args.find(i) != mask_args.end()) {
                command_str += "***";
            } else {
                command_str += cmd[i];
            }
        }
        SWSS_LOG_DEBUG("Executing command: %s", command_str.c_str());

        // Convert to char* array for posix_spawn
        std::vector<char*> argv;
        for (const auto& arg : cmd) {
            argv.push_back(const_cast<char*>(arg.c_str()));
        }
        argv.push_back(nullptr);

        // Use posix_spawn for direct execution without shell
        pid_t pid;
        int result = posix_spawn(&pid, argv[0], nullptr, nullptr, argv.data(), environ);
        if (result != 0) {
            SWSS_LOG_ERROR("Failed to spawn command: %s (error: %s)", command_str.c_str(), strerror(result));
            return false;
        }

        int status;
        waitpid(pid, &status, 0);
        bool success = WIFEXITED(status) && WEXITSTATUS(status) == 0;

        if (!success) {
            SWSS_LOG_ERROR("Command failed with status %d: %s", status, command_str.c_str());
        }

        return success;
    }
};

struct UserInfo {
    std::string username;
    std::string role;
    std::string password_hash;
    std::vector<std::string> ssh_keys;
    bool enabled;
    uid_t uid;
    gid_t gid;
    std::string home_dir;
    std::string shell;

    // Comparison operator for detecting changes
    bool operator==(const UserInfo& other) const {
        return role == other.role &&
               password_hash == other.password_hash &&
               ssh_keys == other.ssh_keys &&
               enabled == other.enabled &&
               shell == other.shell;
        // Note: We don't compare username, uid, gid, home_dir as these are identity fields
    }

    bool operator!=(const UserInfo& other) const {
        return !(*this == other);
    }
};

struct SecurityPolicy {
    std::string role;
    int max_login_attempts;
};

class UserManager {
private:
    std::shared_ptr<swss::DBConnector> m_config_db;
    std::map<std::string, UserInfo> m_users;
    std::map<std::string, SecurityPolicy> m_security_policies;
    bool m_feature_enabled;

public:
    UserManager() : m_feature_enabled(false) {
        m_config_db = std::make_shared<swss::DBConnector>("CONFIG_DB", 0);
    }

    bool is_feature_enabled() {
        swss::Table device_metadata_table(m_config_db.get(), DEVICE_METADATA_TABLE);
        std::vector<swss::KeyOpFieldsValuesTuple> metadata_data;
        device_metadata_table.getContent(metadata_data);

        for (const auto& entry : metadata_data) {
            std::string key = kfvKey(entry);
            if (key == DEVICE_METADATA_LOCALHOST_KEY) {
                auto fvs = kfvFieldsValues(entry);
                for (const auto& fv : fvs) {
                    if (fvField(fv) == LOCAL_USER_MANAGEMENT_FIELD) {
                        return fvValue(fv) == "enabled";
                    }
                }
            }
        }
        // Default to disabled if not explicitly set
        return false;
    }

    bool is_valid_ssh_key(const std::string& key) {
        if (key.empty()) {
            return false;
        }

        // Check if it starts with a known SSH key type
        if (key.find("ssh-") != 0 && key.find("ecdsa-") != 0 &&
            key.find("ed25519") == std::string::npos && key.find("rsa") == std::string::npos) {
            return false;
        }

        // Check if it has at least 3 parts (type, key, comment)
        std::istringstream iss(key);
        std::string part;
        int part_count = 0;
        while (iss >> part && part_count < 3) {
            part_count++;
        }

        return part_count >= 2; // At minimum: type and key (comment is optional)
    }

    void parse_ssh_keys_string(const std::string& keys_str, std::vector<std::string>& ssh_keys) {
        if (keys_str.empty()) {
            return;
        }

        if (keys_str.find(',') != std::string::npos) {
            // Comma-separated keys
            std::stringstream ss(keys_str);
            std::string key;
            int valid_count = 0;

            while (std::getline(ss, key, ',')) {
                // Trim whitespace
                key.erase(0, key.find_first_not_of(" \t\n\r"));
                key.erase(key.find_last_not_of(" \t\n\r") + 1);

                if (is_valid_ssh_key(key)) {
                    ssh_keys.push_back(key);
                    valid_count++;
                }
            }
            SWSS_LOG_DEBUG("Parsed %d valid SSH keys from comma-separated string", valid_count);
        } else {
            // Single key
            if (is_valid_ssh_key(keys_str)) {
                ssh_keys.push_back(keys_str);
                SWSS_LOG_DEBUG("Parsed 1 valid SSH key from string");
            } else {
                SWSS_LOG_WARN("Invalid SSH key format in string");
            }
        }
    }

    void update_user_ssh_keys(UserInfo& user, const std::string& field_value, const std::string& username) {
        // Parse SSH keys - handle both JSON array and comma-separated string formats
        if (field_value.empty()) {
            SWSS_LOG_DEBUG("Skipping empty SSH keys field for user %s", username.c_str());
            return;
        }

        // Try to parse as JSON first
        nlohmann::json ssh_keys_json;
        try {
            ssh_keys_json = nlohmann::json::parse(field_value);
        } catch (const std::exception& e) {
            // Not valid JSON, parse as plain string (comma-separated or single key)
            SWSS_LOG_DEBUG("SSH keys not in JSON format for user %s, parsing as string", username.c_str());
            parse_ssh_keys_string(field_value, user.ssh_keys);
            return;
        }

        // Successfully parsed as JSON, now check the type
        if (ssh_keys_json.is_array()) {
            // Handle JSON array format: ["key1", "key2"]
            for (const auto& key : ssh_keys_json) {
                if (key.is_string()) {
                    std::string key_str = key.get<std::string>();
                    if (is_valid_ssh_key(key_str)) {
                        user.ssh_keys.push_back(key_str);
                    }
                }
            }
            SWSS_LOG_DEBUG("Parsed %zu SSH keys from JSON array for user %s", user.ssh_keys.size(), username.c_str());
        } else if (ssh_keys_json.is_string()) {
            // Handle JSON string format: "key1" or "key1,key2"
            std::string keys_str = ssh_keys_json.get<std::string>();
            parse_ssh_keys_string(keys_str, user.ssh_keys);
            SWSS_LOG_DEBUG("Parsed SSH keys from JSON string for user %s", username.c_str());
        } else {
            SWSS_LOG_WARN("SSH keys field is not a JSON array or string for user %s", username.c_str());
        }
    }

    void load_config() {
        m_feature_enabled = is_feature_enabled();

        if (!m_feature_enabled) {
            SWSS_LOG_INFO("Local user management is disabled, skipping config load");
            return;
        }

        // Load users
        swss::Table user_table(m_config_db.get(), LOCAL_USER_TABLE);
        std::vector<swss::KeyOpFieldsValuesTuple> user_data;
        user_table.getContent(user_data);
        m_users.clear();

        for (const auto& entry : user_data) {
            UserInfo user;
            user.username = kfvKey(entry);
            auto fvs = kfvFieldsValues(entry);

            for (const auto& field : fvs) {
                std::string field_name = fvField(field);
                std::string field_value = fvValue(field);

                if (field_name == "role") {
                    user.role = field_value;
                } else if (field_name == "password_hash") {
                    user.password_hash = field_value;
                } else if (field_name == "enabled") {
                    user.enabled = (field_value == "true" || field_value == "True");
                } else if (field_name == "ssh_keys") {
                    update_user_ssh_keys(user, field_value, user.username);
                }
            }

            m_users[user.username] = user;
        }

        // Load security policies
        swss::Table policy_table(m_config_db.get(), LOCAL_ROLE_SECURITY_POLICY_TABLE);
        std::vector<swss::KeyOpFieldsValuesTuple> policy_data;
        policy_table.getContent(policy_data);
        m_security_policies.clear();

        for (const auto& entry : policy_data) {
            SecurityPolicy policy;
            policy.role = kfvKey(entry);
            auto fvs = kfvFieldsValues(entry);

            for (const auto& field : fvs) {
                std::string field_name = fvField(field);
                std::string field_value = fvValue(field);

                if (field_name == "max_login_attempts") {
                    policy.max_login_attempts = std::stoi(field_value);
                }
            }

            m_security_policies[policy.role] = policy;
        }

        SWSS_LOG_INFO("Loaded %zu users and %zu security policies from CONFIG_DB",
                      m_users.size(), m_security_policies.size());
    }

    uid_t get_next_available_uid() {
        std::set<uid_t> used_uids;

        // Get all existing UIDs
        setpwent();
        struct passwd* pw;
        while ((pw = getpwent()) != nullptr) {
            used_uids.insert(pw->pw_uid);
        }
        endpwent();

        // Find next available UID
        for (uid_t uid = MIN_USER_UID; uid <= MAX_USER_UID; ++uid) {
            if (used_uids.find(uid) == used_uids.end()) {
                return uid;
            }
        }

        SWSS_LOG_ERROR("No available UIDs in range %d-%d", MIN_USER_UID, MAX_USER_UID);
        return 0; // Invalid UID
    }

    std::map<std::string, UserInfo> get_existing_users() {
        std::map<std::string, UserInfo> users;

        setpwent();
        struct passwd* pw;
        while ((pw = getpwent()) != nullptr) {
            // Skip system users and users outside our UID range
            if (SYSTEM_USERS.find(pw->pw_name) != SYSTEM_USERS.end() ||
                pw->pw_uid < MIN_USER_UID || pw->pw_uid > MAX_USER_UID) {
                continue;
            }

            UserInfo user;
            user.username = pw->pw_name;
            user.uid = pw->pw_uid;
            user.gid = pw->pw_gid;
            user.home_dir = pw->pw_dir;
            user.shell = pw->pw_shell;
            user.enabled = (std::string(pw->pw_shell) != "/usr/sbin/nologin");
            user.role = get_user_role_from_groups(pw->pw_name);

            users[user.username] = user;
        }
        endpwent();

        return users;
    }

    std::vector<std::string> get_user_groups(const std::string& username) {
        std::vector<std::string> groups;

        // Get user info
        struct passwd* pw = getpwnam(username.c_str());
        if (!pw) return groups;

        // Check all groups
        setgrent();
        struct group* gr;
        while ((gr = getgrent()) != nullptr) {
            // Check if user is in this group
            for (char** member = gr->gr_mem; *member; ++member) {
                if (username == *member) {
                    groups.push_back(gr->gr_name);
                    break;
                }
            }

            // Also check primary group
            if (gr->gr_gid == pw->pw_gid) {
                if (find(groups.begin(), groups.end(), gr->gr_name) == groups.end()) {
                    groups.push_back(gr->gr_name);
                }
            }
        }
        endgrent();

        return groups;
    }

    std::string get_user_role_from_groups(const std::string& username) {
        std::vector<std::string> user_groups = get_user_groups(username);
        std::set<std::string> user_groups_set(user_groups.begin(), user_groups.end());

        // Check each role to see if user has all required groups for that role
        for (const auto& role_entry : ROLE_GROUPS) {
            const std::string& role = role_entry.first;
            const std::vector<std::string>& required_groups = role_entry.second;

            bool has_all_groups = true;
            for (const std::string& group : required_groups) {
                if (user_groups_set.find(group) == user_groups_set.end()) {
                    has_all_groups = false;
                    break;
                }
            }

            if (has_all_groups) {
                return role;
            }
        }

        // If no role matches, return empty string
        return "";
    }

    bool create_user(const std::string& username, const UserInfo& user_config) {
        bool user_created = false;
        try {
            // Get next available UID
            uid_t uid = get_next_available_uid();
            if (uid == 0) {
                SWSS_LOG_ERROR("Failed to get UID for user %s", username.c_str());
                return false;
            }

            // Validate role
            if (ROLE_GROUPS.find(user_config.role) == ROLE_GROUPS.end()) {
                SWSS_LOG_ERROR("Invalid role %s for user %s", user_config.role.c_str(), username.c_str());
                return false;
            }

            // Create user with useradd
            std::string home_dir = "/home/" + username;
            std::string shell = user_config.enabled ? "/bin/bash" : "/usr/sbin/nologin";

            std::vector<std::string> cmd = {
                "/usr/sbin/useradd", "-u", std::to_string(uid), "-d", home_dir,
                "-m", "-s", shell, username
            };

            if (!SystemCommand::execute(cmd)) {
                SWSS_LOG_ERROR("Failed to create user %s", username.c_str());
                return false;
            }
            user_created = true;

            // Set password hash
            if (!set_user_password(username, user_config.password_hash)) {
                SWSS_LOG_ERROR("Failed to set password for user %s", username.c_str());
                goto cleanup_user;
            }

            // Add user to role groups
            if (!set_user_groups(username, user_config.role)) {
                SWSS_LOG_ERROR("Failed to set groups for user %s", username.c_str());
                goto cleanup_user;
            }

            // Set up SSH keys
            if (!user_config.ssh_keys.empty()) {
                if (!setup_ssh_keys(username, user_config.ssh_keys)) {
                    SWSS_LOG_ERROR("Failed to setup SSH keys for user %s", username.c_str());
                    goto cleanup_user;
                }
            }

            syslog(LOG_INFO, "Successfully created user %s with role %s",
                   username.c_str(), user_config.role.c_str());
            return true;

        cleanup_user:
            if (user_created) {
                SWSS_LOG_WARN("Cleaning up partially created user %s", username.c_str());
                delete_user(username);
            }
            return false;

        } catch (const std::exception& e) {
            syslog(LOG_ERR, "Failed to create user %s: %s", username.c_str(), e.what());
            if (user_created) {
                SWSS_LOG_WARN("Cleaning up partially created user %s due to exception", username.c_str());
                delete_user(username);
            }
            return false;
        }
    }

    bool delete_user(const std::string& username) {
        std::vector<std::string> cmd = {"/usr/sbin/userdel", "-r", username};

        if (!SystemCommand::execute(cmd)) {
            SWSS_LOG_ERROR("Failed to delete user %s", username.c_str());
            return false;
        }

        syslog(LOG_INFO, "Successfully deleted user %s", username.c_str());
        return true;
    }

    bool unmanage_user(const std::string& username) {
        // Remove user from managed group to indicate they're no longer managed
        if (is_user_managed(username)) {
            std::vector<std::string> cmd = {"/usr/sbin/gpasswd", "-d", username, MANAGED_USER_GROUP};
            if (!SystemCommand::execute(cmd)) {
                SWSS_LOG_ERROR("Failed to remove user %s from managed group", username.c_str());
                return false;
            }
            SWSS_LOG_INFO("Removed user %s from managed group %s", username.c_str(), MANAGED_USER_GROUP.c_str());
        } else {
            SWSS_LOG_DEBUG("User %s is not in managed group", username.c_str());
        }

        syslog(LOG_INFO, "Successfully unmanaged user %s (user account preserved)", username.c_str());
        return true;
    }

    bool set_user_password(const std::string& username, const std::string& password_hash) {
        std::vector<std::string> cmd = {"/usr/sbin/usermod", "-p", password_hash, username};

        // Mask the password hash argument (index 2) in logs
        if (!SystemCommand::execute(cmd, {2})) {
            SWSS_LOG_ERROR("Failed to set password for user %s", username.c_str());
            return false;
        }

        SWSS_LOG_DEBUG("Updated password for user %s", username.c_str());
        return true;
    }

    bool set_user_shell(const std::string& username, bool enabled) {
        std::string shell = enabled ? "/bin/bash" : "/usr/sbin/nologin";
        std::vector<std::string> cmd = {"/usr/sbin/usermod", "-s", shell, username};

        if (!SystemCommand::execute(cmd)) {
            SWSS_LOG_ERROR("Failed to set shell for user %s", username.c_str());
            return false;
        }

        SWSS_LOG_DEBUG("Set shell for user %s to %s", username.c_str(), shell.c_str());
        return true;
    }

    bool set_user_groups(const std::string& username, const std::string& role) {
        // Ensure managed group exists
        if (!ensure_managed_group_exists()) {
            SWSS_LOG_ERROR("Failed to ensure managed group exists for user %s", username.c_str());
            return false;
        }

        // Always add user to managed group first (only if not already a member)
        if (!is_user_managed(username)) {
            std::vector<std::string> managed_cmd = {"/usr/sbin/usermod", "-a", "-G", MANAGED_USER_GROUP, username};
            if (!SystemCommand::execute(managed_cmd)) {
                SWSS_LOG_ERROR("Failed to add user %s to managed group", username.c_str());
                return false;
            }
            SWSS_LOG_DEBUG("Added user %s to managed group", username.c_str());
        } else {
            SWSS_LOG_DEBUG("User %s already in managed group", username.c_str());
        }

        auto it = ROLE_GROUPS.find(role);
        if (it == ROLE_GROUPS.end()) {
            SWSS_LOG_WARN("No groups defined for role %s", role.c_str());
            return true;
        }

        // Get all role-based groups that this user should NOT be in
        std::set<std::string> groups_to_remove;
        std::set<std::string> new_role_groups(it->second.begin(), it->second.end());

        for (const auto& role_entry : ROLE_GROUPS) {
            if (role_entry.first != role) {
                // This is a different role, check if user is in any of its groups
                for (const std::string& group : role_entry.second) {
                    // Only remove if the group is not also part of the new role
                    if (new_role_groups.find(group) == new_role_groups.end() &&
                        is_user_in_group(username, group)) {
                        groups_to_remove.insert(group);
                    }
                }
            }
        }

        // Remove user from groups they should no longer be in
        for (const std::string& group : groups_to_remove) {
            std::vector<std::string> cmd = {"/usr/sbin/gpasswd", "-d", username, group};
            if (SystemCommand::execute(cmd)) {
                SWSS_LOG_DEBUG("Removed user %s from group %s", username.c_str(), group.c_str());
            } else {
                SWSS_LOG_WARN("Failed to remove user %s from group %s", username.c_str(), group.c_str());
            }
        }

        // Add user to role-specific groups
        for (const std::string& group : it->second) {
            if (!is_user_in_group(username, group)) {
                std::vector<std::string> cmd = {"/usr/sbin/usermod", "-a", "-G", group, username};
                SystemCommand::execute(cmd); // Don't fail if group doesn't exist
                SWSS_LOG_DEBUG("Added user %s to group %s", username.c_str(), group.c_str());
            } else {
                SWSS_LOG_DEBUG("User %s already in group %s", username.c_str(), group.c_str());
            }
        }

        SWSS_LOG_DEBUG("Updated user %s groups for role %s", username.c_str(), role.c_str());
        return true;
    }

    bool setup_ssh_keys(const std::string& username, const std::vector<std::string>& ssh_keys) {
        try {
            std::string home_dir = "/home/" + username;
            std::string ssh_dir = home_dir + "/.ssh";
            std::string authorized_keys_file = ssh_dir + "/authorized_keys";

            // Create .ssh directory
            std::vector<std::string> mkdir_cmd = {"/usr/bin/mkdir", "-p", ssh_dir};
            if (!SystemCommand::execute(mkdir_cmd)) {
                SWSS_LOG_ERROR("Failed to create SSH directory for user %s", username.c_str());
                return false;
            }

            // Write SSH keys
            std::ofstream file(authorized_keys_file);
            if (!file.is_open()) {
                SWSS_LOG_ERROR("Failed to open authorized_keys file for user %s", username.c_str());
                return false;
            }

            for (const std::string& key : ssh_keys) {
                file << key << "\n";
            }
            file.close();

            // Set proper ownership and permissions
            struct passwd* pw = getpwnam(username.c_str());
            if (!pw) {
                SWSS_LOG_ERROR("Failed to get user info for %s", username.c_str());
                return false;
            }

            std::string chown("/usr/bin/chown");
            std::string chmod("/usr/bin/chmod");

            std::vector<std::string> chown_dir_cmd = {chown, std::to_string(pw->pw_uid) + ":" + std::to_string(pw->pw_gid), ssh_dir};
            std::vector<std::string> chown_file_cmd = {chown, std::to_string(pw->pw_uid) + ":" + std::to_string(pw->pw_gid), authorized_keys_file};
            std::vector<std::string> chmod_dir_cmd = {chmod, "700", ssh_dir};
            std::vector<std::string> chmod_file_cmd = {chmod, "600", authorized_keys_file};

            if (!SystemCommand::execute(chown_dir_cmd)) {
                SWSS_LOG_ERROR("Failed to set ownership of SSH directory for user %s", username.c_str());
                return false;
            }

            if (!SystemCommand::execute(chown_file_cmd)) {
                SWSS_LOG_ERROR("Failed to set ownership of authorized_keys file for user %s", username.c_str());
                return false;
            }

            if (!SystemCommand::execute(chmod_dir_cmd)) {
                SWSS_LOG_ERROR("Failed to set permissions on SSH directory for user %s", username.c_str());
                return false;
            }

            if (!SystemCommand::execute(chmod_file_cmd)) {
                SWSS_LOG_ERROR("Failed to set permissions on authorized_keys file for user %s", username.c_str());
                return false;
            }

            SWSS_LOG_DEBUG("Set up %zu SSH keys for user %s", ssh_keys.size(), username.c_str());
            return true;

        } catch (const std::exception& e) {
            syslog(LOG_ERR, "Failed to setup SSH keys for user %s: %s", username.c_str(), e.what());
            return false;
        }
    }

    bool ensure_managed_group_exists() {
        // Check if group already exists
        struct group* grp = getgrnam(MANAGED_USER_GROUP.c_str());
        if (grp != nullptr) {
            SWSS_LOG_DEBUG("Managed group %s already exists", MANAGED_USER_GROUP.c_str());
            return true;
        }

        // Create the managed group
        std::vector<std::string> cmd = {"/usr/sbin/groupadd", MANAGED_USER_GROUP};
        if (!SystemCommand::execute(cmd)) {
            SWSS_LOG_ERROR("Failed to create managed group %s", MANAGED_USER_GROUP.c_str());
            return false;
        }

        SWSS_LOG_INFO("Created managed group %s", MANAGED_USER_GROUP.c_str());
        return true;
    }

    bool is_user_in_group(const std::string& username, const std::string& groupname) {
        // Get user's groups
        struct passwd* pw = getpwnam(username.c_str());
        if (!pw) {
            return false;
        }

        // Check primary group
        struct group* primary_grp = getgrgid(pw->pw_gid);
        if (primary_grp && std::string(primary_grp->gr_name) == groupname) {
            return true;
        }

        // Check supplementary groups
        int ngroups = 0;
        getgrouplist(username.c_str(), pw->pw_gid, nullptr, &ngroups);

        if (ngroups > 0) {
            std::vector<gid_t> groups(ngroups);
            if (getgrouplist(username.c_str(), pw->pw_gid, groups.data(), &ngroups) != -1) {
                for (gid_t gid : groups) {
                    struct group* grp = getgrgid(gid);
                    if (grp && std::string(grp->gr_name) == groupname) {
                        return true;
                    }
                }
            }
        }

        return false;
    }

    bool is_user_managed(const std::string& username) {
        return is_user_in_group(username, MANAGED_USER_GROUP);
    }

    void perform_consistency_check() {
        if (!m_feature_enabled) {
            SWSS_LOG_INFO("Feature disabled, skipping consistency check");
            return;
        }

        SWSS_LOG_DEBUG("Performing startup consistency check...");

        // Get existing system users
        auto system_users = get_existing_users();

        // Get users that should exist according to CONFIG_DB
        std::set<std::string> config_users;
        for (const auto& user : m_users) {
            config_users.insert(user.first);
        }

        // Ensure all CONFIG_DB users exist and are properly configured
        for (const auto& user_entry : m_users) {
            const std::string& username = user_entry.first;
            const UserInfo& user_config = user_entry.second;

            if (system_users.find(username) == system_users.end()) {
                SWSS_LOG_INFO("Creating missing user: %s", username.c_str());
                create_user(username, user_config);
            } else {
                // Update existing user configuration
                update_user(username, user_config);
            }
        }

        // Find managed users that exist in system but not in CONFIG_DB
        std::set<std::string> unmanaged_users;
        for (const auto& user : system_users) {
            const std::string& username = user.first;
            // Skip if user is in CONFIG_DB
            if (config_users.find(username) != config_users.end()) {
                continue;
            }
            // Skip system users
            if (SYSTEM_USERS.find(username) != SYSTEM_USERS.end()) {
                continue;
            }
            // Only consider users that are managed by userd
            if (is_user_managed(username)) {
                unmanaged_users.insert(username);
            }
        }

        // Remove unmanaged users that were previously managed by userd
        for (const std::string& username : unmanaged_users) {
            SWSS_LOG_INFO("Removing previously managed user: %s", username.c_str());
            delete_user(username);
        }

        SWSS_LOG_INFO("Consistency check completed");
    }

    bool update_user(const std::string& username, const UserInfo& user_config) {
        // Get current user info from system
        auto current_users = get_existing_users();
        auto current_it = current_users.find(username);

        if (current_it == current_users.end()) {
            SWSS_LOG_ERROR("User %s not found for update", username.c_str());
            return false;
        }

        const UserInfo& current_info = current_it->second;

        // Create expected UserInfo with correct shell based on enabled status
        UserInfo expected_config = user_config;
        expected_config.shell = user_config.enabled ? "/bin/bash" : "/usr/sbin/nologin";

        // Compare the configurations
        if (current_info == expected_config) {
            SWSS_LOG_DEBUG("User %s configuration is already up to date", username.c_str());
            return true;
        }

        // Configuration differs, apply updates
        if (!user_config.password_hash.empty() &&
            current_info.password_hash != user_config.password_hash) {
            if (!set_user_password(username, user_config.password_hash)) {
                return false;
            }
        }

        if (current_info.shell != expected_config.shell) {
            if (!set_user_shell(username, user_config.enabled)) {
                return false;
            }
        }

        if (!user_config.role.empty() && current_info.role != user_config.role) {
            SWSS_LOG_INFO("Changing user %s role from '%s' to '%s'",
                         username.c_str(), current_info.role.c_str(), user_config.role.c_str());
            if (!set_user_groups(username, user_config.role)) {
                return false;
            }
        }

        if (current_info.ssh_keys != user_config.ssh_keys) {
            if (!setup_ssh_keys(username, user_config.ssh_keys)) {
                return false;
            }
        }

        SWSS_LOG_INFO("Updated user %s", username.c_str());
        return true;
    }

    void update_security_policies() {
        if (!m_feature_enabled) {
            return;
        }

        update_pam_faillock();
        SWSS_LOG_INFO("Security policies updated");
    }

    void update_pam_faillock() {
        try {
            // Create JSON object with security policies for the template
            nlohmann::json template_data;
            nlohmann::json policies;

            for (const auto& policy : m_security_policies) {
                policies[policy.second.role]["max_login_attempts"] = policy.second.max_login_attempts;
            }

            template_data["security_policies"] = policies;

            // Write JSON to temporary file
            std::string temp_json_file = "/tmp/security_policies.json";
            std::ofstream json_file(temp_json_file);
            if (!json_file.is_open()) {
                SWSS_LOG_ERROR("Failed to create temporary JSON file for template");
                return;
            }

            json_file << template_data.dump(2); // Pretty print with 2-space indentation
            json_file.close();

            // Render template using the JSON file
            std::string j2_command = "j2 " + PAM_FAILLOCK_TEMPLATE + " " + temp_json_file;
            std::string rendered_content;
            FILE* pipe = popen(j2_command.c_str(), "r");
            if (!pipe) {
                SWSS_LOG_ERROR("Failed to execute j2 template rendering");
                unlink(temp_json_file.c_str());
                return;
            }

            char buffer[256];
            while (fgets(buffer, sizeof(buffer), pipe) != nullptr) {
                rendered_content += buffer;
            }

            int status = pclose(pipe);
            if (status != 0) {
                SWSS_LOG_ERROR("j2 template rendering failed with status %d", status);
                unlink(temp_json_file.c_str());
                return;
            }

            // Write rendered content to final config file
            std::ofstream file(PAM_FAILLOCK_CONF);
            if (!file.is_open()) {
                SWSS_LOG_ERROR("Failed to open PAM faillock config file");
                unlink(temp_json_file.c_str());
                return;
            }

            file << rendered_content;
            file.close();

            // Set proper permissions
            std::vector<std::string> chmod_cmd = {"/usr/bin/chmod", "644", PAM_FAILLOCK_CONF};
            if (!SystemCommand::execute(chmod_cmd)) {
                SWSS_LOG_ERROR("Failed to set permissions on PAM faillock config file");
                unlink(temp_json_file.c_str());
                return;
            }

            // Clean up temporary file
            unlink(temp_json_file.c_str());

            SWSS_LOG_INFO("Updated PAM faillock configuration using template");

        } catch (const std::exception& e) {
            SWSS_LOG_ERROR("Failed to update PAM faillock: %s", e.what());
        }
    }

    void clear_all_managed_data() {
        SWSS_LOG_INFO("Clearing all managed users and policies");

        // Unmanage all managed users (preserve user accounts)
        // Note: Using unmanage_user instead of delete_user
        // to preserve user data when feature is disabled
        for (const auto& user_pair : m_users) {
            const std::string& username = user_pair.first;
            syslog(LOG_INFO, "Unmanaging user: %s", username.c_str());
            unmanage_user(username);
        }

        // Clear internal state
        m_users.clear();
        m_security_policies.clear();

        SWSS_LOG_INFO("Successfully cleared all managed data");
    }

    void handle_config_change(const std::string& table, const std::string& key, const std::map<std::string, std::string>& data) {
        if (table == LOCAL_USER_TABLE) {
            if (!data.empty()) {
                // User added or modified
                UserInfo user;
                user.username = key;

                for (const auto& field : data) {
                    if (field.first == "role") {
                        user.role = field.second;
                    } else if (field.first == "password_hash") {
                        user.password_hash = field.second;
                    } else if (field.first == "enabled") {
                        user.enabled = (field.second == "true" || field.second == "True");
                    } else if (field.first == "ssh_keys") {
                        update_user_ssh_keys(user, field.second, key);
                    }
                }

                auto existing_users = get_existing_users();
                if (existing_users.find(key) != existing_users.end()) {
                    update_user(key, user);
                } else {
                    create_user(key, user);
                }

                m_users[key] = user;
            } else {
                // User deleted
                delete_user(key);
                m_users.erase(key);
            }

        } else if (table == LOCAL_ROLE_SECURITY_POLICY_TABLE) {
            if (!data.empty()) {
                SecurityPolicy policy;
                policy.role = key;

                for (const auto& field : data) {
                    if (field.first == "max_login_attempts") {
                        policy.max_login_attempts = std::stoi(field.second);
                    }
                }

                m_security_policies[key] = policy;
            } else {
                m_security_policies.erase(key);
            }

            update_security_policies();
        } else if (table == DEVICE_METADATA_TABLE && key == DEVICE_METADATA_LOCALHOST_KEY) {
            bool new_state = false;
            for (const auto& field : data) {
                if (field.first == LOCAL_USER_MANAGEMENT_FIELD && field.second == "enabled") {
                    new_state = true;
                    break;
                }
            }

            if (new_state != m_feature_enabled) {
                m_feature_enabled = new_state;
                syslog(LOG_INFO, "Local user management %s",
                       m_feature_enabled ? "enabled" : "disabled");

                if (m_feature_enabled) {
                    // Feature enabled - reload config and perform consistency check
                    load_config();
                    perform_consistency_check();
                    update_security_policies();
                } else {
                    // Feature disabled - clear all managed users and policies
                    clear_all_managed_data();
                }
            }
        }
    }
};

int main() {
    // Set up signal handlers
    signal(SIGHUP, signal_handler);
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    // Initialize syslog
    openlog("userd", LOG_PID, LOG_DAEMON);
    SWSS_LOG_INFO("userd daemon starting...");

    try {
        UserManager user_manager;
        user_manager.load_config();

        // Perform initial consistency check if feature is enabled
        user_manager.perform_consistency_check();

        // Update security policies
        user_manager.update_security_policies();

        // Set up CONFIG_DB monitoring
        swss::DBConnector config_db("CONFIG_DB", 0);

        // Subscribe to table changes
        swss::SubscriberStateTable user_table(&config_db, LOCAL_USER_TABLE);
        swss::SubscriberStateTable policy_table(&config_db, LOCAL_ROLE_SECURITY_POLICY_TABLE);
        swss::SubscriberStateTable device_metadata_table(&config_db, DEVICE_METADATA_TABLE);

        swss::Select s;
        s.addSelectable(&user_table);
        s.addSelectable(&policy_table);
        s.addSelectable(&device_metadata_table);

        SWSS_LOG_INFO("userd daemon started successfully");

        // Main daemon loop
        while (!g_shutdown) {
            swss::Selectable *sel;
            int ret = s.select(&sel, 1000); // 1 second timeout

            if (ret == swss::Select::ERROR) {
                SWSS_LOG_ERROR("Select error in daemon loop");
                break;
            } else if (ret == swss::Select::TIMEOUT) {
                continue;
            }

            try {
                if (sel == &user_table) {
                    swss::KeyOpFieldsValuesTuple kco;
                    user_table.pop(kco);

                    std::string key = kfvKey(kco);
                    std::string op = kfvOp(kco);
                    auto fvs = kfvFieldsValues(kco);

                    std::map<std::string, std::string> data;
                    if (op == "SET") {
                        for (const auto& fv : fvs) {
                            data[fvField(fv)] = fvValue(fv);
                        }
                    }

                    user_manager.handle_config_change(LOCAL_USER_TABLE, key, data);

                } else if (sel == &policy_table) {
                    swss::KeyOpFieldsValuesTuple kco;
                    policy_table.pop(kco);

                    std::string key = kfvKey(kco);
                    std::string op = kfvOp(kco);
                    auto fvs = kfvFieldsValues(kco);

                    std::map<std::string, std::string> data;
                    if (op == "SET") {
                        for (const auto& fv : fvs) {
                            data[fvField(fv)] = fvValue(fv);
                        }
                    }

                    user_manager.handle_config_change(LOCAL_ROLE_SECURITY_POLICY_TABLE, key, data);

                } else if (sel == &device_metadata_table) {
                    swss::KeyOpFieldsValuesTuple kco;
                    device_metadata_table.pop(kco);

                    std::string key = kfvKey(kco);
                    std::string op = kfvOp(kco);
                    auto fvs = kfvFieldsValues(kco);

                    if (key == DEVICE_METADATA_LOCALHOST_KEY) {
                        std::map<std::string, std::string> data;
                        if (op == "SET") {
                            for (const auto& fv : fvs) {
                                data[fvField(fv)] = fvValue(fv);
                            }
                        }

                        user_manager.handle_config_change(DEVICE_METADATA_TABLE, key, data);
                    }
                }

            } catch (const std::exception& e) {
                SWSS_LOG_ERROR("Error in daemon loop: %s", e.what());
            }
        }

        SWSS_LOG_INFO("userd daemon shutting down...");

    } catch (const std::exception& e) {
        SWSS_LOG_ERROR("userd daemon failed: %s", e.what());
        closelog();
        return 1;
    }

    closelog();
    return 0;
}
