/*
 * Host-specific iMessage helper launcher
 *
 * FDA-bearing launcher for the Python worker. Installers bake in every trusted
 * code path and the separate runtime bridge path. The hardened installer sets
 * EXPECTED_CODE_UID=0 and REQUIRE_ROOT_POLICY=1.
 *
 * Dual-mode build:
 * - Baked mode (default): Compile with -DHELPER_SCRIPT, -DSEND_GATE_SCRIPT,
 *   -DCONFIRM_HELPER, -DBRIDGE_ROOT. Paths are baked at compile time and the
 *   exec arguments stay byte-identical with the pre-product wrapper.
 * - Product mode: Compile with -DIMESSAGE_PRODUCT_BUILD=1. Paths resolved
 *   at runtime via --product <id> CLI. Requires product allowlist match and
 *   runs the bundled interpreter with -I -B.
 */

#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <pwd.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#ifdef IMESSAGE_PRODUCT_BUILD
#ifdef __APPLE__
#include <CoreFoundation/CoreFoundation.h>
#include <mach-o/dyld.h>
#include <Security/Security.h>
#else
extern int _NSGetExecutablePath(char *buf, uint32_t *bufsize);
#endif
#endif

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

#ifdef IMESSAGE_PRODUCT_BUILD
#ifndef APP_SUPPORT_DIRNAME
#error "APP_SUPPORT_DIRNAME must be defined for product build"
#endif

#ifndef PYTHON_RELPATH
#error "PYTHON_RELPATH must be defined for product build"
#endif

#ifndef IMESSAGE_BUNDLE_ID
#error "IMESSAGE_BUNDLE_ID must be defined for product build"
#endif

#ifndef IMESSAGE_CONFIRM_BUNDLE_ID
#error "IMESSAGE_CONFIRM_BUNDLE_ID must be defined for product build"
#endif

#ifndef IMESSAGE_PYTHON_BUNDLE_ID
#error "IMESSAGE_PYTHON_BUNDLE_ID must be defined for product build"
#endif

#ifndef IMESSAGE_TEAM_ID
#error "IMESSAGE_TEAM_ID must be defined for product build"
#endif

#ifndef IMESSAGE_BUNDLE_REQUIREMENT
#define IMESSAGE_BUNDLE_REQUIREMENT ""
#endif

#ifndef IMESSAGE_CONFIRM_REQUIREMENT
#define IMESSAGE_CONFIRM_REQUIREMENT ""
#endif

#ifndef IMESSAGE_PYTHON_REQUIREMENT
#define IMESSAGE_PYTHON_REQUIREMENT ""
#endif
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

/*
 * Product validation exit codes:
 * 2 = required artifact missing, 3 = symlink/non-regular artifact,
 * 4 = owner mismatch, 5 = group/world writable, 6 = not executable,
     * 7 = derived path/env too long,
     * 8 = CLI/allowlist usage error, 9 = bundle/home discovery failure,
     * 10 = code signature / seal validation failure.
 */
static int validate_ownership(const char *path, const char *label, uid_t current_uid,
                               uid_t bundle_owner, bool reject_symlink,
                               bool require_executable) {
    struct stat st;
    if (lstat(path, &st) != 0) {
        fprintf(stderr, "%s: %s missing at %s (%s)\n", HELPER_DISPLAY_NAME,
                label, path, strerror(errno));
        return 2;
    }
    if (reject_symlink && S_ISLNK(st.st_mode)) {
        fprintf(stderr, "%s: %s %s is a symlink; refusing\n",
                HELPER_DISPLAY_NAME, label, path);
        return 3;
    }
    if (!S_ISREG(st.st_mode)) {
        fprintf(stderr, "%s: %s %s is not a regular file; refusing\n",
                HELPER_DISPLAY_NAME, label, path);
        return 3;
    }
    if (st.st_mode & (S_IWGRP | S_IWOTH)) {
        fprintf(stderr, "%s: %s %s is group/world writable; refusing\n",
                HELPER_DISPLAY_NAME, label, path);
        return 5;
    }
    (void)current_uid;
    if (st.st_uid != bundle_owner && st.st_uid != 0) {
        fprintf(stderr,
                "%s: %s %s has uid %u, expected 0 or bundle owner %u; refusing\n",
                HELPER_DISPLAY_NAME, label, path, (unsigned int)st.st_uid,
                (unsigned int)bundle_owner);
        return 4;
    }
    if (require_executable && !(st.st_mode & (S_IXUSR | S_IXGRP | S_IXOTH))) {
        fprintf(stderr, "%s: %s %s is not executable; refusing\n",
                HELPER_DISPLAY_NAME, label, path);
        return 6;
    }
    if (require_executable && access(path, X_OK) != 0) {
        fprintf(stderr, "%s: %s %s is not executable by current user; refusing (%s)\n",
                HELPER_DISPLAY_NAME, label, path, strerror(errno));
        return 6;
    }
    return 0;
}

#if defined(IMESSAGE_PRODUCT_BUILD) && defined(__APPLE__)
static void print_cf_error(const char *label, OSStatus status, CFErrorRef error) {
    fprintf(stderr, "%s: %s code signature validation failed (%d)",
            HELPER_DISPLAY_NAME, label, (int)status);
    if (error) {
        CFStringRef description = CFErrorCopyDescription(error);
        if (description) {
            char buffer[1024];
            if (CFStringGetCString(description, buffer, sizeof(buffer),
                                   kCFStringEncodingUTF8)) {
                fprintf(stderr, ": %s", buffer);
            }
            CFRelease(description);
        }
    }
    fputc('\n', stderr);
}

static CFStringRef create_requirement_string(const char *identifier,
                                             const char *override_requirement) {
    char requirement[1024];
    const char *source = override_requirement;
    if (!source || source[0] == '\0') {
        int written = snprintf(requirement, sizeof(requirement),
                               "anchor apple generic and identifier \"%s\" "
                               "and certificate leaf[subject.OU] = \"%s\"",
                               identifier, IMESSAGE_TEAM_ID);
        if (written < 0 || (size_t)written >= sizeof(requirement)) {
            fprintf(stderr, "%s: code requirement is too long\n",
                    HELPER_DISPLAY_NAME);
            return NULL;
        }
        source = requirement;
    }
    return CFStringCreateWithCString(NULL, source, kCFStringEncodingUTF8);
}

static int validate_code_requirement(const char *path, const char *label,
                                     const char *identifier,
                                     const char *override_requirement,
                                     bool is_directory, bool check_nested) {
    CFURLRef url = CFURLCreateFromFileSystemRepresentation(
        NULL, (const UInt8 *)path, strlen(path), is_directory);
    if (!url) {
        fprintf(stderr, "%s: cannot create code URL for %s\n",
                HELPER_DISPLAY_NAME, label);
        return 10;
    }

    SecStaticCodeRef code = NULL;
    OSStatus status = SecStaticCodeCreateWithPath(url, kSecCSDefaultFlags, &code);
    CFRelease(url);
    if (status != errSecSuccess || !code) {
        print_cf_error(label, status, NULL);
        return 10;
    }

    CFStringRef requirement_string =
        create_requirement_string(identifier, override_requirement);
    if (!requirement_string) {
        CFRelease(code);
        return 10;
    }

    SecRequirementRef requirement = NULL;
    status = SecRequirementCreateWithString(requirement_string,
                                            kSecCSDefaultFlags, &requirement);
    CFRelease(requirement_string);
    if (status != errSecSuccess || !requirement) {
        print_cf_error(label, status, NULL);
        CFRelease(code);
        return 10;
    }

    SecCSFlags flags =
        kSecCSStrictValidate | kSecCSCheckAllArchitectures | kSecCSNoNetworkAccess;
    if (check_nested) {
        flags |= kSecCSCheckNestedCode;
    }

    CFErrorRef error = NULL;
    status = SecStaticCodeCheckValidityWithErrors(code, flags, requirement, &error);
    if (status != errSecSuccess) {
        print_cf_error(label, status, error);
        if (error) {
            CFRelease(error);
        }
        CFRelease(requirement);
        CFRelease(code);
        return 10;
    }

    if (error) {
        CFRelease(error);
    }
    CFRelease(requirement);
    CFRelease(code);
    return 0;
}
#else
static int validate_code_requirement(const char *path, const char *label,
                                     const char *identifier,
                                     const char *override_requirement,
                                     bool is_directory, bool check_nested) {
    (void)path;
    (void)label;
    (void)identifier;
    (void)override_requirement;
    (void)is_directory;
    (void)check_nested;
    return 0;
}
#endif

static int set_env_value(char *buffer, size_t size, const char *name,
                         const char *value) {
    int written = snprintf(buffer, size, "%s=%s", name, value);
    if (written < 0 || (size_t)written >= size) {
        fprintf(stderr, "%s: %s value is too long\n", HELPER_DISPLAY_NAME, name);
        return -1;
    }
    return 0;
}

static int format_path(char *buffer, size_t size, const char *label,
                       const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    int written = vsnprintf(buffer, size, fmt, args);
    va_end(args);
    if (written < 0 || (size_t)written >= size) {
        fprintf(stderr, "%s: %s path is too long\n", HELPER_DISPLAY_NAME, label);
        return 7;
    }
    return 0;
}

static void print_json_string(const char *value) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)value; *p; p++) {
        switch (*p) {
            case '"':
                fputs("\\\"", stdout);
                break;
            case '\\':
                fputs("\\\\", stdout);
                break;
            case '\b':
                fputs("\\b", stdout);
                break;
            case '\f':
                fputs("\\f", stdout);
                break;
            case '\n':
                fputs("\\n", stdout);
                break;
            case '\r':
                fputs("\\r", stdout);
                break;
            case '\t':
                fputs("\\t", stdout);
                break;
            default:
                if (*p < 0x20) {
                    printf("\\u%04x", *p);
                } else {
                    putchar(*p);
                }
        }
    }
    putchar('"');
}

static void print_json_field(const char *name, const char *value) {
    print_json_string(name);
    putchar(':');
    print_json_string(value);
}

static int get_bundle_path(char *bundle_path, size_t bundle_path_size) {
    char exe_path[PATH_MAX];
    uint32_t size = sizeof(exe_path);
    if (_NSGetExecutablePath(exe_path, &size) != 0) {
        fprintf(stderr, "%s: executable path too long\n", HELPER_DISPLAY_NAME);
        return 9;
    }

    char real_path[PATH_MAX];
    if (realpath(exe_path, real_path) == NULL) {
        fprintf(stderr, "%s: realpath failed: %s\n", HELPER_DISPLAY_NAME,
                strerror(errno));
        return 9;
    }

    char real_path_copy[PATH_MAX];
    strncpy(real_path_copy, real_path, sizeof(real_path_copy) - 1);
    real_path_copy[sizeof(real_path_copy) - 1] = '\0';

    char *base = basename(real_path_copy);
    size_t base_len = strlen(base);
    size_t real_len = strlen(real_path);
    const char *expected_suffix = "/Contents/Helpers/";
    size_t suffix_len = strlen(expected_suffix);

    if (real_len <= base_len + suffix_len) {
        fprintf(stderr, "%s: not inside Contents/Helpers/\n", HELPER_DISPLAY_NAME);
        return 9;
    }

    size_t prefix_len = real_len - base_len;
    if (strncmp(real_path + prefix_len - suffix_len, expected_suffix, suffix_len) != 0) {
        fprintf(stderr, "%s: not inside Contents/Helpers/\n", HELPER_DISPLAY_NAME);
        return 9;
    }

    char work_path[PATH_MAX];
    strncpy(work_path, real_path, sizeof(work_path) - 1);
    work_path[sizeof(work_path) - 1] = '\0';

    char *d1 = dirname(work_path);
    char temp1[PATH_MAX];
    strncpy(temp1, d1, sizeof(temp1) - 1);
    temp1[sizeof(temp1) - 1] = '\0';

    char *d2 = dirname(temp1);
    char temp2[PATH_MAX];
    strncpy(temp2, d2, sizeof(temp2) - 1);
    temp2[sizeof(temp2) - 1] = '\0';

    char *d3 = dirname(temp2);

    if (strlen(d3) + 5 > bundle_path_size) {
        fprintf(stderr, "%s: bundle path too long\n", HELPER_DISPLAY_NAME);
        return 9;
    }

    int ret = format_path(bundle_path, bundle_path_size, "bundle", "%s", d3);
    if (ret != 0) {
        return ret;
    }

    char info_plist[PATH_MAX];
    ret = format_path(info_plist, sizeof(info_plist), "Info.plist",
                      "%s/Contents/Info.plist", bundle_path);
    if (ret != 0) {
        return ret;
    }
    struct stat st;
    if (stat(info_plist, &st) != 0) {
        fprintf(stderr, "%s: Contents/Info.plist missing\n", HELPER_DISPLAY_NAME);
        return 9;
    }

    return 0;
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

    char bundle_path[PATH_MAX];
    int ret = get_bundle_path(bundle_path, sizeof(bundle_path));
    if (ret != 0) {
        return ret;
    }

    uid_t current_uid = getuid();
    struct stat bundle_st;
    if (stat(bundle_path, &bundle_st) != 0) {
        fprintf(stderr, "%s: cannot stat bundle: %s\n", HELPER_DISPLAY_NAME,
                strerror(errno));
        return 9;
    }
    uid_t bundle_owner = bundle_st.st_uid;
    if (bundle_owner != current_uid && bundle_owner != 0) {
        fprintf(stderr,
                "%s: bundle owner %u is neither current user %u nor root; refusing\n",
                HELPER_DISPLAY_NAME, (unsigned int)bundle_owner,
                (unsigned int)current_uid);
        return 4;
    }

    struct passwd *pw = getpwuid(current_uid);
    if (!pw || !pw->pw_dir) {
        fprintf(stderr, "%s: cannot get home directory\n", HELPER_DISPLAY_NAME);
        return 9;
    }

    char bridge_root[PATH_MAX];
    ret = format_path(bridge_root, sizeof(bridge_root), "bridge root",
                      "%s/Library/Application Support/%s/bridges/%s",
                      pw->pw_dir, APP_SUPPORT_DIRNAME, entry->product_id);
    if (ret != 0) return ret;

    char policy_dir[PATH_MAX] = "";
    bool is_host = strcmp(entry->role, "host") == 0;
    if (is_host) {
        ret = format_path(policy_dir, sizeof(policy_dir), "policy dir",
                          "%s/Library/Application Support/%s/policies/%s",
                          pw->pw_dir, APP_SUPPORT_DIRNAME, entry->product_id);
        if (ret != 0) return ret;
    }

    char helper_py[PATH_MAX];
    ret = format_path(helper_py, sizeof(helper_py), "helper.py",
                      "%s/Contents/Resources/core/bin/helper.py", bundle_path);
    if (ret != 0) return ret;

    char send_gate_py[PATH_MAX];
    ret = format_path(send_gate_py, sizeof(send_gate_py), "send_gate.py",
                      "%s/Contents/Resources/core/bin/send_gate.py", bundle_path);
    if (ret != 0) return ret;

    char confirm_helper[PATH_MAX];
    ret = format_path(confirm_helper, sizeof(confirm_helper), "confirm helper",
                      "%s/Contents/Helpers/imessage-confirm", bundle_path);
    if (ret != 0) return ret;

    /* Host app icon for the native confirmation alert: a helper outside
     * Contents/MacOS has no bundle icon of its own, so the confirm helper is
     * told where the product's icon lives. Optional at runtime. */
    char host_icon[PATH_MAX];
    ret = format_path(host_icon, sizeof(host_icon), "host icon",
                      "%s/Contents/Resources/AppIcon.icns", bundle_path);
    if (ret != 0) return ret;
    struct stat host_icon_st;
    bool host_icon_available = true;
    if (lstat(host_icon, &host_icon_st) != 0) {
        if (errno == ENOENT) {
            host_icon_available = false;
        } else {
            fprintf(stderr, "%s: cannot inspect host icon at %s (%s)\n",
                    HELPER_DISPLAY_NAME, host_icon, strerror(errno));
            return 2;
        }
    }

    char python_interp[PATH_MAX];
    ret = format_path(python_interp, sizeof(python_interp), "Python interpreter",
                      "%s/Contents/Frameworks/Python.framework/%s",
                      bundle_path, PYTHON_RELPATH);
    if (ret != 0) return ret;

    ret = validate_ownership(helper_py, "helper.py", current_uid, bundle_owner, true, false);
    if (ret != 0) return ret;

    ret = validate_ownership(send_gate_py, "send_gate.py", current_uid, bundle_owner, true, false);
    if (ret != 0) return ret;

    ret = validate_ownership(confirm_helper, "confirm helper", current_uid, bundle_owner, true, true);
    if (ret != 0) return ret;

    if (host_icon_available) {
        ret = validate_ownership(host_icon, "host icon", current_uid, bundle_owner, true, false);
        if (ret != 0) return ret;
    }

    ret = validate_ownership(python_interp, "Python interpreter", current_uid, bundle_owner, true, true);
    if (ret != 0) return ret;

    ret = validate_code_requirement(bundle_path, "app bundle", IMESSAGE_BUNDLE_ID,
                                    IMESSAGE_BUNDLE_REQUIREMENT, true, true);
    if (ret != 0) return ret;

    ret = validate_code_requirement(confirm_helper, "confirm helper",
                                    IMESSAGE_CONFIRM_BUNDLE_ID,
                                    IMESSAGE_CONFIRM_REQUIREMENT, false, false);
    if (ret != 0) return ret;

    ret = validate_code_requirement(python_interp, "Python interpreter",
                                    IMESSAGE_PYTHON_BUNDLE_ID,
                                    IMESSAGE_PYTHON_REQUIREMENT, false, false);
    if (ret != 0) return ret;

    if (validate_only) {
        putchar('{');
        print_json_field("product", entry->product_id);
        putchar(',');
        print_json_field("role", entry->role);
        putchar(',');
        print_json_field("bridge_root", bridge_root);
        if (is_host) {
            putchar(',');
            print_json_field("policy_dir", policy_dir);
        }
        putchar(',');
        print_json_field("helper_py", helper_py);
        putchar(',');
        print_json_field("send_gate_py", send_gate_py);
        putchar(',');
        print_json_field("confirm_helper", confirm_helper);
        putchar(',');
        print_json_field("host_icon", host_icon);
        putchar(',');
        print_json_field("python_interp", python_interp);
        puts("}");
        return 0;
    }

    char tmpdir[PATH_MAX];
#ifdef __APPLE__
    size_t tmpdir_len = confstr(_CS_DARWIN_USER_TEMP_DIR, tmpdir, sizeof(tmpdir));
    if (tmpdir_len == 0 || tmpdir_len > sizeof(tmpdir)) {
        strcpy(tmpdir, "/tmp");
    } else {
        size_t len = strlen(tmpdir);
        if (len > 0 && tmpdir[len - 1] == '/') {
            tmpdir[len - 1] = '\0';
        }
    }
#else
    strcpy(tmpdir, "/tmp");
#endif

    static char env_path[] = "PATH=/usr/bin:/bin";
    static char env_home[PATH_MAX + 16];
    static char env_lang[] = "LANG=en_US.UTF-8";
    static char env_tmpdir[PATH_MAX + 16];
    static char env_bridge_dir[PATH_MAX + 48];
    static char env_product_id[128];
    static char env_role[64];
    static char env_policy_dir[PATH_MAX + 48];
    static char env_confirm_path[PATH_MAX + 64];
    static char env_send_gate_path[PATH_MAX + 64];
    static char env_host_display[128];
    static char env_host_icon[PATH_MAX + 48];

    if (set_env_value(env_home, sizeof(env_home), "HOME", pw->pw_dir) != 0 ||
        set_env_value(env_tmpdir, sizeof(env_tmpdir), "TMPDIR", tmpdir) != 0 ||
        set_env_value(env_bridge_dir, sizeof(env_bridge_dir), "IMESSAGE_BRIDGE_DIR",
                      bridge_root) != 0 ||
        set_env_value(env_product_id, sizeof(env_product_id), "IMESSAGE_PRODUCT_ID",
                      entry->product_id) != 0 ||
        set_env_value(env_role, sizeof(env_role), "IMESSAGE_BRIDGE_ROLE",
                      entry->role) != 0 ||
        set_env_value(env_confirm_path, sizeof(env_confirm_path),
                      "IMESSAGE_CONFIRM_HELPER_PATH", confirm_helper) != 0 ||
        set_env_value(env_send_gate_path, sizeof(env_send_gate_path),
                      "IMESSAGE_SEND_GATE_PATH", send_gate_py) != 0 ||
        set_env_value(env_host_display, sizeof(env_host_display),
                      "IMESSAGE_HOST_DISPLAY_NAME", entry->host_display_name) != 0) {
        return 7;
    }

    if (host_icon_available &&
        set_env_value(env_host_icon, sizeof(env_host_icon),
                      "IMESSAGE_HOST_ICON_PATH", host_icon) != 0) {
        return 7;
    }

    if (is_host && set_env_value(env_policy_dir, sizeof(env_policy_dir),
                                  "IMESSAGE_POLICY_DIR", policy_dir) != 0) {
        return 7;
    }

    static char *new_env_host[] = {
        env_path,
        env_home,
        env_lang,
        env_tmpdir,
        env_bridge_dir,
        env_product_id,
        env_role,
        env_policy_dir,
        env_confirm_path,
        env_send_gate_path,
        env_host_display,
        NULL,
        NULL,
    };

    static char *new_env_manager[] = {
        env_path,
        env_home,
        env_lang,
        env_tmpdir,
        env_bridge_dir,
        env_product_id,
        env_role,
        env_confirm_path,
        env_send_gate_path,
        env_host_display,
        NULL,
        NULL,
    };

    if (host_icon_available) {
        new_env_host[(sizeof(new_env_host) / sizeof(new_env_host[0])) - 2] = env_host_icon;
        new_env_manager[(sizeof(new_env_manager) / sizeof(new_env_manager[0])) - 2] = env_host_icon;
    }

    environ = is_host ? new_env_host : new_env_manager;

    char *exec_argv[] = {
        python_interp,
        "-I",
        "-B",
        helper_py,
        NULL,
    };

    execv(python_interp, exec_argv);

    fprintf(stderr, "%s: execv %s failed: %s\n", HELPER_DISPLAY_NAME,
            python_interp, strerror(errno));
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
