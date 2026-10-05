#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/reboot.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t cancelled = 0;
static void cancel_guard(int sig) { (void)sig; cancelled = 1; }

int main(int argc, char **argv) {
    if (argc != 4) return 64;
    char *end = NULL;
    long delay = strtol(argv[1], &end, 10);
    if (!end || *end || delay < 1 || delay > 3600) return 64;
    int dry_run = strcmp(argv[3], "dry-run") == 0;
    if (!dry_run && strcmp(argv[3], "reboot") != 0) return 64;
    struct sigaction sa = {0};
    sa.sa_handler = cancel_guard;
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGTERM, &sa, NULL) || sigaction(SIGINT, &sa, NULL)) return 65;
    FILE *pidfile = fopen(argv[2], "w");
    if (!pidfile) return 66;
    fprintf(pidfile, "%ld\n", (long)getpid());
    if (fclose(pidfile)) return 67;
    struct timespec deadline;
    if (clock_gettime(CLOCK_MONOTONIC, &deadline)) return 68;
    deadline.tv_sec += delay;
    while (!cancelled) {
        int rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &deadline, NULL);
        if (rc == EINTR) continue;
        if (rc != 0) return 69;
        if (cancelled) break;
        if (dry_run) { puts("dry-run deadline reached"); return 0; }
        if (reboot(RB_AUTOBOOT)) return 70;
    }
    if (dry_run) puts("dry-run cancelled");
    return 0;
}
