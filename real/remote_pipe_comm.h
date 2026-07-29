#pragma once

// Optional remote backend for the upstream PipeComm API. When neither
// LEGOSIM_PIPE_GATEWAY nor LEGOSIM_FIFO_BROKER is set, callers retain the
// original named-FIFO path. LEGOSIM_PIPE_GATEWAY selects the official
// SimBricks BaseIf-backed gateway; LEGOSIM_FIFO_BROKER is retained only for
// backwards-compatible diagnostics. Both endpoints use HOST:PORT and the
// same length-delimited TCP protocol at this boundary.

#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>

namespace InterChiplet {
inline const char* remotePipeEndpoint() {
    const char* simbricks_gateway = std::getenv("LEGOSIM_PIPE_GATEWAY");
    return simbricks_gateway != NULL ? simbricks_gateway : std::getenv("LEGOSIM_FIFO_BROKER");
}

inline bool remotePipeEnabled() { return remotePipeEndpoint() != NULL; }

// Pin loads SIFT tools in a restricted linker namespace.  Do not use libc's
// socket, connect, send, recv, close, getaddrinfo, inet_pton, or htons here:
// their dynamic symbols are unavailable to that namespace.  The recorder
// already uses syscall(2), which remains available to the Pin tool.
inline int remotePipeSocket() {
    return static_cast<int>(::syscall(SYS_socket, AF_INET, SOCK_STREAM, 0));
}

inline int remotePipeConnectSystemCall(int socket_fd, const sockaddr_in& address) {
    return static_cast<int>(::syscall(
        SYS_connect, socket_fd, &address, static_cast<socklen_t>(sizeof(address))));
}

inline ssize_t remotePipeSend(int socket_fd, const void* buffer, std::size_t bytes) {
    return static_cast<ssize_t>(
        ::syscall(SYS_sendto, socket_fd, buffer, bytes, MSG_NOSIGNAL, NULL, 0));
}

inline ssize_t remotePipeReceive(int socket_fd, void* buffer, std::size_t bytes) {
    return static_cast<ssize_t>(
        ::syscall(SYS_recvfrom, socket_fd, buffer, bytes, 0, NULL, NULL));
}

inline void remotePipeClose(int socket_fd) { ::syscall(SYS_close, socket_fd); }

inline bool remotePipeParseIPv4(const std::string& text, in_addr_t* address) {
    unsigned int octets[4] = {0, 0, 0, 0};
    std::size_t octet_index = 0;
    unsigned int value = 0;

    for (std::size_t index = 0; index < text.size(); ++index) {
        const char character = text[index];
        if (character == '.') {
            if (octet_index == 3 || value > 255) return false;
            octets[octet_index++] = value;
            value = 0;
        } else if (character >= '0' && character <= '9') {
            value = value * 10U + static_cast<unsigned int>(character - '0');
            if (value > 255) return false;
        } else {
            return false;
        }
    }
    if (octet_index != 3 || value > 255) return false;
    octets[3] = value;

    // sockaddr_in stores bytes in network order. On the little-endian Linux
    // hosts supported by the image, this integer produces a.b.c.d in memory.
    *address = static_cast<in_addr_t>(octets[0] | (octets[1] << 8U) |
                                      (octets[2] << 16U) | (octets[3] << 24U));
    return true;
}

inline in_port_t remotePipePortToNetworkOrder(unsigned short port) {
    return static_cast<in_port_t>((port << 8U) | (port >> 8U));
}

inline int remotePipeConnect() {
    const char* endpoint = remotePipeEndpoint();
    if (endpoint == NULL) {
        std::cerr << "Neither LEGOSIM_PIPE_GATEWAY nor LEGOSIM_FIFO_BROKER is set." << std::endl;
        return -1;
    }
    std::string value(endpoint);
    const std::size_t separator = value.rfind(':');
    if (separator == std::string::npos) {
        std::cerr << "Pipe gateway endpoint must be HOST:PORT." << std::endl;
        return -1;
    }
    // The Python worker resolves the Swarm service name before launching a
    // simulator, so this hot path only receives an IPv4 literal.
    sockaddr_in address = {};
    address.sin_family = AF_INET;
    const int port = std::atoi(value.substr(separator + 1).c_str());
    if (port < 1 || port > 65535 ||
        !remotePipeParseIPv4(value.substr(0, separator), &address.sin_addr.s_addr)) {
        std::cerr << "Pipe gateway must be an IPv4 literal." << std::endl;
        return -1;
    }
    address.sin_port = remotePipePortToNetworkOrder(static_cast<unsigned short>(port));
    int socket_fd = remotePipeSocket();
    if (socket_fd >= 0 && remotePipeConnectSystemCall(socket_fd, address) != 0) {
        remotePipeClose(socket_fd);
        socket_fd = -1;
    }
    if (socket_fd < 0) std::cerr << "Cannot connect to Pipe gateway." << std::endl;
    return socket_fd;
}

inline bool remotePipeWriteAll(int socket_fd, const void* buffer, std::size_t bytes) {
    const char* cursor = static_cast<const char*>(buffer);
    while (bytes > 0) {
        const ssize_t written = remotePipeSend(socket_fd, cursor, bytes);
        if (written <= 0) return false;
        cursor += written;
        bytes -= static_cast<std::size_t>(written);
    }
    return true;
}

inline bool remotePipeReadAll(int socket_fd, void* buffer, std::size_t bytes) {
    char* cursor = static_cast<char*>(buffer);
    while (bytes > 0) {
        const ssize_t received = remotePipeReceive(socket_fd, cursor, bytes);
        if (received <= 0) return false;
        cursor += received;
        bytes -= static_cast<std::size_t>(received);
    }
    return true;
}

inline std::string remotePipeReadLine(int socket_fd) {
    std::string line;
    char byte = 0;
    while (remotePipeReceive(socket_fd, &byte, 1) == 1) {
        if (byte == '\n') return line;
        line.push_back(byte);
        if (line.size() > 256) return std::string();
    }
    return std::string();
}

inline std::string remotePipeNumber(int value) {
    std::ostringstream stream;
    stream << value;
    return stream.str();
}

inline int remotePipeWrite(const char* name, const void* buffer, int bytes) {
    const int socket_fd = remotePipeConnect();
    if (socket_fd < 0) return -1;
    const std::string header = "W " + std::string(name) + " " + remotePipeNumber(bytes) + "\n";
    const bool success = remotePipeWriteAll(socket_fd, header.data(), header.size()) &&
                         remotePipeWriteAll(socket_fd, buffer, static_cast<std::size_t>(bytes)) &&
                         remotePipeReadLine(socket_fd) == "OK";
    remotePipeClose(socket_fd);
    return success ? bytes : -1;
}

inline int remotePipeRead(const char* name, void* buffer, int bytes) {
    const int socket_fd = remotePipeConnect();
    if (socket_fd < 0) return -1;
    const std::string expected = "OK " + remotePipeNumber(bytes);
    const std::string header = "R " + std::string(name) + " " + remotePipeNumber(bytes) + "\n";
    const bool success = remotePipeWriteAll(socket_fd, header.data(), header.size()) &&
                         remotePipeReadLine(socket_fd) == expected &&
                         remotePipeReadAll(socket_fd, buffer, static_cast<std::size_t>(bytes));
    remotePipeClose(socket_fd);
    return success ? bytes : -1;
}
}  // namespace InterChiplet
