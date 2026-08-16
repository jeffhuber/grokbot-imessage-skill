/*
 * Host-specific iMessage helper launcher
 *
 * FDA-bearing launcher for the Python worker. Installers bake in every trusted
 * code path and the separate runtime bridge path. The hardened installer sets
 * EXPECTED_CODE_UID=0 and REQUIRE_ROOT_POLICY=1.
 *
 * Dual-mode build:
 * - Baked mode (default): Compile with -DHELPER_SCRIPT, -DSEND_GATE_SCRIPT,
 *   -DCONFIRM_HELPER, -DBRIDGE_ROOT. Paths are baked at compile time.
 * - Product mode: Compile with -DIMESSAGE_PRODUCT_BUILD=1. Paths resolved
 *   at runtime via --product <id> CLI. Requires product allowlist match.
 */

#include <errno.h>
#include <limits.h>
#include <pwd.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#ifdef IMESSAGE_PRODUCT_BUILD
    #if defined(HELPER_SCRIPT) || defined(SEND_GATE_SCRIPT) || \
        defined(CONFIRM_HELPER) || defined(BRIDGE_ROOT)
        #error "Product build mode (-DIMESSAGE_PRODUCT_BUILD) and baked path macros are mutually exclusive"
    #endif
#else
    #ifndef HELPER_SCRIPT
    #error "HELPER_SCRIPT must be defined at build time"
    #endif

    #ifndef SEND_GATE_SCRIPT
    #error "SEND_GATE_SCRIPT must be defined at build time"
    #endif

    #ifndef CONFIRM_HELPER
    #error "CONFIRM_HELPER must be defined at build time"
    #endif

    #ifndef BRIDGE_ROOT
    #error "BRIDGE_ROOT must be defined at build time"
    #endif
#endif

#ifndef PYTHON_INTERPRETER
#define PYTHON_INTERPRETER "/usr/bin/python3"
#endif

#ifndef EXPECTED_CODE_UID
#define EXPECTED_CODE_UID -1L
#endif

#ifndef READ_POLICY_MODE
#define READ_POLICY_MODE "runtime"
#endif

#ifndef READ_ALLOWLIST_PATH
#define READ_ALLOWLIST_PATH BRIDGE_ROOT "/contacts/allowed_chats.txt"
#endif

#ifndef REQUIRE_ROOT_POLICY
#define REQUIRE_ROOT_POLICY 0
#endif

#ifndef HELPER_DISPLAY_NAME
#define HELPER_DISPLAY_NAME "imessage-helper"
#endif

#ifndef HOST_DISPLAY_NAME
#define HOST_DISPLAY_NAME "AI assistant"
#endif

extern char **environ;

#ifdef IMESSAGE_PRODUCT_BUILD
typedef struct {
    const char *product_id;
    const char *host_display_name;
    const char *role;
} product_entry;

static const product_entry product_allowlist[] = {
    {"claude", "Claude", "host"},
    {"grok", "Grok", "host"},
    {"openai", "ChatGPT", "host"},
    {"manager", "Manager", "manager"},
};

static const size_t product_allowlist_size =
    sizeof(product_allowlist) / sizeof(product_allowlist[0]);

static const product_entry *find_product(const char *product_id) {
    if (!product_id) {
        return NULL;
    }
    for (size_t i = 0; i < product_allowlist_size; i++) {
        if (strcmp(product_id, product_allowlist[i].product_id) == 0) {
            return &product_allowlist[i];
        }
    }
    return NULL;
}

static bool is_path_like(const char *str) {
    return str && (strchr(str, '/') != NULL || strcmp(str, ".") == 0 ||
                   strcmp(str, "..") == 0);
}

static void print_usage(const char *display_name) {
    fprintf(stderr, "Usage: %s --product <id> [--validate-only]\n", display_name);
}
#else
static int validate_file(const char *path, const char *label, uid_t owner,
                         bool require_executable) {
    struct stat st;
    if (lstat(path, &st) != 0) {
        fprintf(stderr, "%s: %s missing at %s (%s)\n", HELPER_DISPLAY_NAME,
                label, path, strerror(errno));
        return 2;
    }
    if (!S_ISREG(st.st_mode)) {
        fprintf(stderr,
                "%s: %s %s is not a regular file; refusing\n",
                HELPER_DISPLAY_NAME, label, path);
        return 3;
    }
    if (st.st_uid != owner) {
        fprintf(stderr,
                "%s: %s %s has uid %u, expected %u; refusing\n",
                HELPER_DISPLAY_NAME, label, path, (unsigned int)st.st_uid,
                (unsigned int)owner);
        return 4;
    }
    if (st.st_mode & (S_IWGRP | S_IWOTH)) {
        fprintf(stderr,
                "%s: %s %s is group/world writable; refusing\n",
                HELPER_DISPLAY_NAME, label, path);
        return 5;
    }
    if (require_executable && !(st.st_mode & (S_IXUSR | S_IXGRP | S_IXOTH))) {
        fprintf(stderr,
                "%s: %s %s is not executable; refusing\n",
                HELPER_DISPLAY_NAME, label, path);
        return 6;
    }
    return 0;
}

static int set_env_value(char *buffer, size_t size, const char *name,
                         const char *value) {
    int written = snprintf(buffer, size, "%s=%s", name, value);
    if (written < 0 || (size_t)written >= size) {
        fprintf(stderr, "%s: %s value is too long\n", HELPER_DISPLAY_NAME, name);
        return -1;
    }
    return 0;
}
#endif

int main(int argc, char **argv) {
#ifdef IMESSAGE_PRODUCT_BUILD
    const char *product_id = NULL;
    bool validate_only = false;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--product") == 0) {
            if (product_id || i + 1 >= argc) {
                print_usage(HELPER_DISPLAY_NAME);
                return 8;
            }
            product_id = argv[++i];
        } else if (strcmp(argv[i], "--validate-only") == 0) {
            if (validate_only) {
                print_usage(HELPER_DISPLAY_NAME);
                return 8;
            }
            validate_only = true;
        } else {
            print_usage(HELPER_DISPLAY_NAME);
            return 8;
        }
    }

    if (!product_id) {
        print_usage(HELPER_DISPLAY_NAME);
        return 8;
    }

    if (is_path_like(product_id)) {
        print_usage(HELPER_DISPLAY_NAME);
        return 8;
    }

    const product_entry *entry = find_product(product_id);
    if (!entry) {
        print_usage(HELPER_DISPLAY_NAME);
        return 8;
    }

    if (validate_only) {
        printf("{\"product\":\"%s\",\"role\":\"%s\",\"validate_only\":true}\n",
               entry->product_id, entry->role);
        return 0;
    }

    fprintf(stderr, "%s: product mode path derivation not yet implemented\n",
            HELPER_DISPLAY_NAME);
    return 1;

#else
    (void)argc;
    (void)argv;

    uid_t expected_code_uid =
        EXPECTED_CODE_UID < 0 ? getuid() : (uid_t)EXPECTED_CODE_UID;
    int validation = validate_file(HELPER_SCRIPT, "helper script",
                                   expected_code_uid, false);
    if (validation != 0) {
        return validation;
    }
    validation = validate_file(SEND_GATE_SCRIPT, "send-gate module",
                               expected_code_uid, false);
    if (validation != 0) {
        return validation;
    }
    validation = validate_file(CONFIRM_HELPER, "confirmation helper",
                               expected_code_uid, true);
    if (validation != 0) {
        return validation;
    }

#if REQUIRE_ROOT_POLICY
    validation = validate_file(READ_ALLOWLIST_PATH, "read allowlist", 0, false);
    if (validation != 0) {
        return validation;
    }
    struct stat policy_st;
    if (lstat(READ_ALLOWLIST_PATH, &policy_st) != 0 ||
        (policy_st.st_mode & (S_IRWXG | S_IRWXO))) {
        fprintf(stderr,
                "%s: read allowlist has group/world permissions; refusing\n",
                HELPER_DISPLAY_NAME);
        return 5;
    }
#endif

    struct passwd *pw = getpwuid(getuid());
    static char home_buf[PATH_MAX + 16];
    static char bridge_buf_new[PATH_MAX + 48];
    static char bridge_buf_old[PATH_MAX + 48];
    static char policy_buf[64];
    static char allowlist_buf[PATH_MAX + 64];
    static char root_policy_buf[64];
    static char host_display_buf[128];

    if (set_env_value(home_buf, sizeof(home_buf), "HOME",
                      pw && pw->pw_dir ? pw->pw_dir : "/") != 0 ||
        set_env_value(bridge_buf_new, sizeof(bridge_buf_new),
                      "IMESSAGE_BRIDGE_DIR", BRIDGE_ROOT) != 0 ||
        set_env_value(bridge_buf_old, sizeof(bridge_buf_old),
                      "COWORK_IMESSAGE_BRIDGE_DIR", BRIDGE_ROOT) != 0 ||
        set_env_value(policy_buf, sizeof(policy_buf),
                      "COWORK_IMESSAGE_READ_POLICY", READ_POLICY_MODE) != 0 ||
        set_env_value(allowlist_buf, sizeof(allowlist_buf),
                      "COWORK_IMESSAGE_READ_ALLOWLIST", READ_ALLOWLIST_PATH) != 0 ||
        set_env_value(root_policy_buf, sizeof(root_policy_buf),
                      "COWORK_IMESSAGE_REQUIRE_ROOT_POLICY",
                      REQUIRE_ROOT_POLICY ? "1" : "0") != 0 ||
        set_env_value(host_display_buf, sizeof(host_display_buf),
                      "IMESSAGE_HOST_DISPLAY_NAME", HOST_DISPLAY_NAME) != 0) {
        return 7;
    }

    static char *new_env[] = {
        "PATH=/usr/bin:/bin",
        home_buf,
        "LANG=en_US.UTF-8",
        bridge_buf_new,
        bridge_buf_old,
        policy_buf,
        allowlist_buf,
        root_policy_buf,
        host_display_buf,
        NULL,
    };
    environ = new_env;

    char *exec_argv[] = {
        (char *)PYTHON_INTERPRETER,
        "-I",
        (char *)HELPER_SCRIPT,
        NULL,
    };
    execv(PYTHON_INTERPRETER, exec_argv);

    fprintf(stderr, "%s: execv %s failed: %s\n", HELPER_DISPLAY_NAME,
            PYTHON_INTERPRETER, strerror(errno));
    return 1;
#endif
}
