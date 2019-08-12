/* Copyright 2019 Lenovo */
#include <arpa/inet.h>
#include <crypt.h>
#include <net/if.h>
#include <netdb.h>
#include <sys/socket.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <sys/types.h>
#include <sys/time.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>

#define MAXPACKET 1024

static const char cryptalpha[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789./";

unsigned char* genpasswd(int len) {
        unsigned char * passwd;
        int urandom;
        passwd = calloc(len + 1, sizeof(char));
        urandom = open("/dev/urandom", O_RDONLY);
        read(urandom, passwd, len);
        close(urandom);
        for (urandom = 0; urandom < len; urandom++) {
                passwd[urandom] = cryptalpha[passwd[urandom] >> 2];
        }
        return passwd;

}


int main(int argc, char* argv[]) {
        int sock;
        unsigned char currlen, currtype;
        unsigned char* passwd;
        unsigned char* cryptedpass;
        unsigned char* macaddr;
        struct timeval timeout;
        struct addrinfo hints;
        struct addrinfo *addrs;
        struct addrinfo *curr;
        struct sockaddr_in net4bind;
        struct sockaddr_in net6bind;
        unsigned char buffer[MAXPACKET];
        memset(&hints, 0, sizeof(struct addrinfo));
        memset(&net4bind, 0, sizeof(struct sockaddr_in));
        memset(&net6bind, 0, sizeof(struct sockaddr_in));
        memset(&buffer, 0, MAXPACKET);
        memset(&timeout, 0, sizeof(struct timeval));
        timeout.tv_sec = 10;
        net4bind.sin_port = htons(302);
        net6bind.sin_port = htons(302);
        hints.ai_socktype = SOCK_STREAM;
        hints.ai_protocol = IPPROTO_TCP;

        passwd = genpasswd(32);
        memset(buffer, 0, MAXPACKET);
        strncpy(buffer, "$5$", 3);
        cryptedpass = genpasswd(8);
        strncpy(buffer + 3, cryptedpass, 8);
        free(cryptedpass);
        cryptedpass = crypt(passwd, buffer);
        if (argc < 3) { 
            fprintf(stderr, "Missing node name and manager\n");
            exit(1);
        }
        sock = getaddrinfo(argv[2], "301", &hints, &addrs);
        if (sock != 0) {
                fprintf(stderr, "Error trying to resolve %s\n", argv[2]);
                exit(1);
        }
        for (curr = addrs; curr != NULL; curr = curr->ai_next) {
                sock = socket(curr->ai_family, curr->ai_socktype, curr->ai_protocol);
                if (sock < 0) continue;
                setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &(int){1}, sizeof(int));
                if (curr->ai_family == AF_INET) {
                        bind(sock, (struct sockaddr*)&net4bind, sizeof(struct sockaddr_in));
                } else if (curr->ai_family == AF_INET6) {
                        bind(sock, (struct sockaddr*)&net6bind, sizeof(struct sockaddr_in6));
                } else {
                        continue;
                }
                if (connect(sock, curr->ai_addr, curr->ai_addrlen) == 0) break;
        }
        if (curr == NULL) {
                fprintf(stderr, "Unable to reach %s\n", argv[2]);
                exit(1);
        }
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        freeaddrinfo(addrs);
        read(sock, buffer, 8);
        if (memcmp(buffer, "\xc2\xd1-\xa8\x80\xd8j\xba", 8) != 0) {
                fprintf(stderr, "Unrecognized server\n");
                exit(1);
        }
        dprintf(sock, "\x01%c%s", strlen(argv[1]), argv[1]);
        write(sock, "\x00\x00", 2);
        memset(buffer, 0, MAXPACKET);
        read(sock, buffer, 2);
        while (buffer[0] != 255) {
            currtype = buffer[0];
            currlen = buffer[1];
            memset(buffer, 0, MAXPACKET);
            if (currlen) {
                read(sock, buffer, currlen);  // Max is 255, well under MAX_PACKET
            }
            if (currtype == 2) {
                dprintf(sock, "\x03%c", currlen);
                write(sock, buffer, currlen);
                dprintf(sock, "\x04%c%s", strlen(cryptedpass), cryptedpass);
                write(sock, "\x00\x00", 2);
            } else if (currtype == 5) {
                printf(passwd);
                printf("\n");
                exit(0);
            }
            buffer[0] = 255;
            read(sock, buffer, 2);
        }
        fprintf(stderr, "Password was not accepted\n");
        exit(1);
}
