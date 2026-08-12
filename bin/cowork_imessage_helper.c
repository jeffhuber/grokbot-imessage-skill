/*
 * cowork-imessage-helper
 *
 * Tiny wrapper that execs the helper.py script in a sanitized environment.
 *
 * Why this exists: Full Disk Access on macOS is granted to a specific binary
 * (identified by code signature). We want FDA to attach to THIS wrapper, not
 * to /usr/bin/python3 — granting FDA to the system Python would give every
 * Python script on the system access to chat.db, Mail, Safari history, etc.
 *
 * Hardening:
 *   - argv is ignored. The helper script scans its own request queue, so the
 *     wrapper does not need to forward any arguments. An attacker who can
 *     trigger the binary cannot pick which file gets read.
 *   - The environment is replaced with a tiny whitelist. DYLD_INSERT_LIBRARIES,
 *     LD_PRELOAD, PYTHONPATH, and friends are dropped, so the helper's grant
 *     cannot be hijacked by injection.
 *   - Python is invoked with -I (isolated mode). This ignores PYTHONPATH,
 *     skips user site-packages, and prevents sys.path[0] from being set to
 *     the script's directory — blocking the "drop a malicious foo.py into
 *     bin/ and watch helper.py import it" attack. The env sanitization is
 *     a belt; -I is the suspenders.
 *   - The helper script path is baked in at build time via -DHELPER_SCRIPT.
 *     Before exec, we stat() it and refuse to run if it is missing, not owned
 *     by the current user, or group/world writable.
 *
 * Build (handled by install.sh):
 *     clang -Wall -Wextra -Werror -O2 \
 *         -DHELPER_SCRIPT='"/abs/path/to/helper.py"' \
 *         -DPYTHON_INTERPRETER='"/usr/bin/python3"' \
 *         -o cowork-imessage-helper cowork_imessage_helper.c
 *     codesign -s - --options runtime cowork-imessage-helper
 */

#include <errno.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef HELPER_SCRIPT
#error "HELPER_SCRIPT must be defined at build time"
#endif

#ifndef PYTHON_INTERPRETER
#define PYTHON_INTERPRETER "/usr/bin/python3"
#endif

extern char **environ;

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;

    /* Validate the helper script before handing it our FDA grant. */
    struct stat st;
    if (stat(HELPER_SCRIPT, &st) != 0) {
        fprintf(stderr,
                "cowork-imessage-helper: helper script missing at %s (%s)\n",
                HELPER_SCRIPT, strerror(errno));
        return 2;
    }
    if (st.st_uid != getuid()) {
        fprintf(stderr,
                "cowork-imessage-helper: helper script %s is not owned by current user; refusing\n",
                HELPER_SCRIPT);
        return 3;
    }
    if (st.st_mode & (S_IWGRP | S_IWOTH)) {
        fprintf(stderr,
                "cowork-imessage-helper: helper script %s is group/world writable; refusing\n",
                HELPER_SCRIPT);
        return 4;
    }

    /* Build a minimal environment. We deliberately drop DYLD_*, LD_*,
     * PYTHON*, and everything else the caller might set. */
    struct passwd *pw = getpwuid(getuid());
    static char home_buf[1024];
    snprintf(home_buf, sizeof(home_buf), "HOME=%s",
             pw && pw->pw_dir ? pw->pw_dir : "/");

    static char *new_env[] = {
        "PATH=/usr/bin:/bin",
        home_buf,
        "LANG=en_US.UTF-8",
        NULL,
    };
    environ = new_env;

    /* -I: isolated mode. Ignores PYTHONPATH, skips user site-packages, and
     * does NOT prepend the script's directory to sys.path. Without this,
     * any .py file a write-happy attacker drops into bin/ could shadow a
     * stdlib import in helper.py. */
    char *exec_argv[] = {
        (char *)PYTHON_INTERPRETER,
        "-I",
        (char *)HELPER_SCRIPT,
        NULL,
    };
    execv(PYTHON_INTERPRETER, exec_argv);

    /* execv only returns on failure. */
    fprintf(stderr, "cowork-imessage-helper: execv %s failed: %s\n",
            PYTHON_INTERPRETER, strerror(errno));
    return 1;
}
