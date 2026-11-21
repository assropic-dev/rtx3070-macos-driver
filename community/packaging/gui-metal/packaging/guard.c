#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    if (argc == 4 && !strcmp(argv[1], "link-new")) {
        if (link(argv[2], argv[3]) == 0) return 0;
        perror("link new LaunchAgent"); return 1;
    }
    if (argc < 5 || strcmp(argv[1], "locked")) return 2;
    int fd = open(argv[2], O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
    if (fd < 0 || flock(fd, LOCK_EX | LOCK_NB) < 0) {
        perror("lock LaunchAgents directory"); return 1;
    }
    argv[2] = "/bin/bash";
    execv(argv[2], argv + 2);
    perror("start preference transaction"); return 1;
}
