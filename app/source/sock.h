// Minimal socket portability layer: libctru (soc:u, BSD-like) on the 3DS, Winsock on the PC test build.
#pragma once

#include <stdint.h>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
typedef SOCKET sock_t;
#define SOCK_INVALID INVALID_SOCKET
#define sock_close closesocket
#define sock_poll WSAPoll
#define sock_errno() WSAGetLastError()
#define SOCK_EWOULDBLOCK WSAEWOULDBLOCK
#define SOCK_EINPROGRESS WSAEWOULDBLOCK
#define SOCK_EINTR WSAEINTR
static inline int sock_set_nonblocking(sock_t s)
{
    u_long on = 1;
    return ioctlsocket(s, FIONBIO, &on);
}
static inline uint64_t plat_ms(void) { return GetTickCount64(); }
#else
#include <3ds.h>
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
typedef int sock_t;
#define SOCK_INVALID (-1)
#define sock_close close
#define sock_poll poll
#define sock_errno() errno
#define SOCK_EWOULDBLOCK EWOULDBLOCK
#define SOCK_EINPROGRESS EINPROGRESS
#define SOCK_EINTR EINTR
static inline int sock_set_nonblocking(sock_t s)
{
    return fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) | O_NONBLOCK);
}
static inline uint64_t plat_ms(void) { return osGetTime(); }
#endif
