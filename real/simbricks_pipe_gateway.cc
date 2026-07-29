/*
 * PipeComm gateway backed by one official SimBricks BaseIf channel.
 *
 * The native LEGOSim recorder speaks the small W/R TCP protocol already used
 * by remote_pipe_comm.h.  A write is forwarded as one BaseIf upper-layer
 * message; a read waits only in the destination gateway's local FIFO.  This
 * keeps the original directed PipeComm semantics while making the data path
 * peer-to-peer instead of using a central Python broker.
 *
 * This binary deliberately implements one peer interface.  The orchestration
 * layer starts one instance per routed peer during the first integration
 * stage, which avoids sharing a BaseIf object across transport threads.
 */

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

extern "C" {
#include <simbricks/base/if.h>
}

#include "simbricks_pipe_protocol.h"

namespace {

constexpr uint32_t kEntryBytes = 65536;
constexpr uint32_t kEntryCount = 128;
constexpr uint8_t kMessageType = SIMBRICKS_PROTO_MSG_TYPE_UPPER_START;
constexpr size_t kBaseHeaderBytes = sizeof(struct SimbricksProtoBaseMsgHeader);
constexpr size_t kFrameBytes = sizeof(SimbricksPipeFrameHeader);

std::atomic<bool> stopping(false);

bool DebugEnabled() {
  const char *value = std::getenv("LEGOSIM_SIMBRICKS_DEBUG");
  return value != nullptr && std::strcmp(value, "1") == 0;
}

void Debug(const std::string &message) {
  if (DebugEnabled()) std::cerr << "pipe-gateway: " << message << '\n';
}

bool ReadAll(int fd, void *buffer, size_t bytes) {
  auto *cursor = static_cast<uint8_t *>(buffer);
  while (bytes != 0) {
    const ssize_t received = recv(fd, cursor, bytes, 0);
    if (received <= 0) return false;
    cursor += received;
    bytes -= static_cast<size_t>(received);
  }
  return true;
}

bool WriteAll(int fd, const void *buffer, size_t bytes) {
  const auto *cursor = static_cast<const uint8_t *>(buffer);
  while (bytes != 0) {
    const ssize_t sent = send(fd, cursor, bytes, MSG_NOSIGNAL);
    if (sent <= 0) return false;
    cursor += sent;
    bytes -= static_cast<size_t>(sent);
  }
  return true;
}

bool ReadLine(int fd, std::string *line) {
  line->clear();
  char character = 0;
  while (line->size() < 512 && ReadAll(fd, &character, 1)) {
    if (character == '\n') return true;
    line->push_back(character);
  }
  return false;
}

struct PendingWrite {
  std::string pipe_name;
  std::vector<uint8_t> payload;
  bool done = false;
  bool success = false;
  std::condition_variable completion;
};

class Gateway {
 public:
  Gateway(SimbricksBaseIf *base, int listen_fd) : base_(base), listen_fd_(listen_fd) {}

  void Run() {
    std::thread acceptor(&Gateway::AcceptLoop, this);
    while (!stopping.load()) {
      ForwardOneWrite();
      ReceiveAll();
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    shutdown(listen_fd_, SHUT_RDWR);
    close(listen_fd_);
    acceptor.join();
  }

 private:
  void AcceptLoop() {
    while (!stopping.load()) {
      int client = accept(listen_fd_, nullptr, nullptr);
      if (client < 0) {
        if (errno == EINTR) continue;
        if (stopping.load()) return;
        perror("accepting PipeComm client");
        continue;
      }
      Debug("accepted PipeComm TCP client");
      std::thread(&Gateway::HandleClient, this, client).detach();
    }
  }

  void HandleClient(int client) {
    std::string line;
    if (!ReadLine(client, &line)) {
      Debug("client closed before a complete PipeComm header");
      close(client);
      return;
    }
    Debug("received PipeComm header: " + line);
    std::istringstream parser(line);
    char operation = 0;
    std::string pipe_name;
    uint32_t bytes = 0;
    std::string unexpected;
    if (!(parser >> operation >> pipe_name >> bytes) || (parser >> unexpected) ||
        (operation != 'W' && operation != 'R') || pipe_name.empty() ||
        pipe_name.size() > 255 || bytes > MaxPayloadBytes()) {
      WriteAll(client, "ERR invalid request\n", 20);
      close(client);
      return;
    }

    if (operation == 'W') {
      std::vector<uint8_t> payload(bytes);
      if (!ReadAll(client, payload.data(), payload.size())) {
        close(client);
        return;
      }
      auto pending = std::make_shared<PendingWrite>();
      pending->pipe_name = pipe_name;
      pending->payload = std::move(payload);
      Debug("queued write pipe=" + pipe_name + " bytes=" + std::to_string(bytes));
      std::unique_lock<std::mutex> lock(mutex_);
      outgoing_.push_back(pending);
      outgoing_ready_.notify_one();
      pending->completion.wait(lock, [&] { return pending->done || stopping.load(); });
      const bool success = pending->success;
      lock.unlock();
      WriteAll(client, success ? "OK\n" : "ERR transport\n", success ? 3 : 14);
    } else {
      std::unique_lock<std::mutex> lock(mutex_);
      incoming_ready_.wait(lock, [&] {
        return stopping.load() || (!incoming_[pipe_name].empty() &&
                                   incoming_[pipe_name].front().size() == bytes);
      });
      if (!stopping.load()) {
        std::vector<uint8_t> payload = std::move(incoming_[pipe_name].front());
        incoming_[pipe_name].pop_front();
        Debug("released read pipe=" + pipe_name + " bytes=" + std::to_string(bytes));
        lock.unlock();
        const std::string response = "OK " + std::to_string(bytes) + "\n";
        if (WriteAll(client, response.data(), response.size()))
          WriteAll(client, payload.data(), payload.size());
      }
    }
    close(client);
  }

  static constexpr size_t MaxPayloadBytes() {
    return kEntryBytes - kBaseHeaderBytes - kFrameBytes - 255;
  }

  void ForwardOneWrite() {
    std::shared_ptr<PendingWrite> pending;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (outgoing_.empty()) return;
      pending = outgoing_.front();
    }
    volatile union SimbricksProtoBaseMsg *message = SimbricksBaseIfOutAlloc(base_, 0);
    if (message == nullptr) return;

    auto *frame = reinterpret_cast<SimbricksPipeFrameHeader *>(
        reinterpret_cast<uint8_t *>(const_cast<union SimbricksProtoBaseMsg *>(message)) +
        kBaseHeaderBytes);
    frame->type = kSimbricksPipeData;
    memset(frame->reserved, 0, sizeof(frame->reserved));
    frame->request_id = 0;
    frame->pipe_name_bytes = htonl(static_cast<uint32_t>(pending->pipe_name.size()));
    frame->payload_bytes = htonl(static_cast<uint32_t>(pending->payload.size()));
    uint8_t *body = reinterpret_cast<uint8_t *>(frame + 1);
    memcpy(body, pending->pipe_name.data(), pending->pipe_name.size());
    memcpy(body + pending->pipe_name.size(), pending->payload.data(), pending->payload.size());
    SimbricksBaseIfOutSend(base_, message, kMessageType);
    Debug("sent BaseIf pipe=" + pending->pipe_name + " bytes=" +
          std::to_string(pending->payload.size()));

    std::lock_guard<std::mutex> lock(mutex_);
    if (!outgoing_.empty() && outgoing_.front() == pending) outgoing_.pop_front();
    pending->success = true;
    pending->done = true;
    pending->completion.notify_one();
  }

  void ReceiveAll() {
    while (true) {
      // InPoll advances BaseIf's input ring position. InPeek is only for a
      // speculative timestamp check; using it here processed slot zero but
      // left in_pos unchanged, making every later message invisible.
      volatile union SimbricksProtoBaseMsg *message = SimbricksBaseIfInPoll(base_, 0);
      if (message == nullptr) return;
      const uint8_t type = SimbricksBaseIfInType(base_, message);
      if (type == SIMBRICKS_PROTO_MSG_TYPE_TERMINATE) {
        SimbricksBaseIfInDone(base_, message);
        stopping.store(true);
        return;
      }
      if (type != kMessageType) {
        std::cerr << "unexpected BaseIf message type " << static_cast<unsigned>(type) << '\n';
        SimbricksBaseIfInDone(base_, message);
        continue;
      }
      const auto *frame = reinterpret_cast<const SimbricksPipeFrameHeader *>(
          reinterpret_cast<const uint8_t *>(const_cast<const union SimbricksProtoBaseMsg *>(message)) +
          kBaseHeaderBytes);
      const uint32_t name_bytes = ntohl(frame->pipe_name_bytes);
      const uint32_t payload_bytes = ntohl(frame->payload_bytes);
      if (frame->type != kSimbricksPipeData || name_bytes == 0 || name_bytes > 255 ||
          payload_bytes > MaxPayloadBytes() ||
          kBaseHeaderBytes + kFrameBytes + name_bytes + payload_bytes > kEntryBytes) {
        std::cerr << "invalid PipeComm BaseIf frame\n";
        SimbricksBaseIfInDone(base_, message);
        continue;
      }
      const uint8_t *body = reinterpret_cast<const uint8_t *>(frame + 1);
      std::string pipe_name(reinterpret_cast<const char *>(body), name_bytes);
      std::vector<uint8_t> payload(body + name_bytes, body + name_bytes + payload_bytes);
      SimbricksBaseIfInDone(base_, message);
      {
        std::lock_guard<std::mutex> lock(mutex_);
        incoming_[pipe_name].push_back(std::move(payload));
      }
      Debug("received BaseIf pipe=" + pipe_name + " bytes=" + std::to_string(payload_bytes));
      incoming_ready_.notify_all();
    }
  }

  SimbricksBaseIf *base_;
  int listen_fd_;
  std::mutex mutex_;
  std::condition_variable outgoing_ready_;
  std::condition_variable incoming_ready_;
  std::deque<std::shared_ptr<PendingWrite>> outgoing_;
  std::map<std::string, std::deque<std::vector<uint8_t>>> incoming_;
};

int OpenTcpListener(uint16_t port) {
  const int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) return -1;
  const int enabled = 1;
  setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));
  sockaddr_in address = {};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_ANY);
  address.sin_port = htons(port);
  if (bind(fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0 ||
      listen(fd, 64) != 0) {
    close(fd);
    return -1;
  }
  return fd;
}

void Usage(const char *program) {
  std::cerr << "Usage: " << program
            << " [--connect] [--ready-file PATH] BASE_SOCKET SHM_PATH TCP_PORT\n"
            << "  Default mode listens for net_sockets on BASE_SOCKET; --connect mode connects.\n";
}

}  // namespace

int main(int argc, char **argv) {
  int argument = 1;
  bool connect_mode = false;
  const char *ready_file = nullptr;
  while (argument < argc && argv[argument][0] == '-') {
    if (std::strcmp(argv[argument], "--connect") == 0) {
      connect_mode = true;
      ++argument;
    } else if (std::strcmp(argv[argument], "--ready-file") == 0 && argument + 1 < argc) {
      ready_file = argv[argument + 1];
      argument += 2;
    } else {
      Usage(argv[0]);
      return EXIT_FAILURE;
    }
  }
  if (argc - argument != 3) {
    Usage(argv[0]);
    return EXIT_FAILURE;
  }
  const int parsed_port = std::atoi(argv[argument + 2]);
  if (parsed_port < 1 || parsed_port > 65535) {
    std::cerr << "TCP_PORT must be in 1..65535\n";
    return EXIT_FAILURE;
  }
  const int listen_fd = OpenTcpListener(static_cast<uint16_t>(parsed_port));
  if (listen_fd < 0) {
    perror("opening PipeComm TCP listener");
    return EXIT_FAILURE;
  }

  SimbricksBaseIfParams params;
  SimbricksBaseIfDefaultParams(&params);
  params.sock_path = argv[argument];
  params.blocking_conn = true;
  params.sync_mode = kSimbricksBaseIfSyncDisabled;
  params.in_entries_size = kEntryBytes;
  params.out_entries_size = kEntryBytes;
  params.in_num_entries = kEntryCount;
  params.out_num_entries = kEntryCount;
  SimbricksBaseIf base;
  SimbricksBaseIfSHMPool pool = {};
  if (SimbricksBaseIfInit(&base, &params) != 0) {
    perror("initializing BaseIf");
    return EXIT_FAILURE;
  }
  if (connect_mode) {
    SimbricksBaseIf *interfaces[] = {&base};
    if (SimbricksBaseIfConnect(&base) != 0 || SimbricksBaseIfConnsWait(interfaces, 1) != 0) {
      perror("connecting BaseIf");
      return EXIT_FAILURE;
    }
  } else if (SimbricksBaseIfSHMPoolCreate(&pool, argv[argument + 1], SimbricksBaseIfSHMSize(&params)) != 0 ||
             SimbricksBaseIfListen(&base, &pool) != 0) {
    perror("listening BaseIf");
    SimbricksBaseIfSHMPoolUnlink(&pool);
    SimbricksBaseIfSHMPoolUnmap(&pool);
    return EXIT_FAILURE;
  }
  if (SimbricksBaseIfIntroSend(&base, nullptr, 0) != 0) {
    perror("sending BaseIf intro");
    return EXIT_FAILURE;
  }
  uint8_t intro[1];
  size_t intro_length = sizeof(intro);
  if (SimbricksBaseIfIntroRecv(&base, intro, &intro_length) != 0) {
    perror("receiving BaseIf intro");
    return EXIT_FAILURE;
  }
  if (ready_file != nullptr) {
    FILE *file = std::fopen(ready_file, "w");
    if (file == nullptr) {
      perror("writing BaseIf ready file");
      return EXIT_FAILURE;
    }
    const bool write_failed = std::fputs("ready\n", file) == EOF;
    const bool close_failed = std::fclose(file) != 0;
    if (write_failed || close_failed) {
      perror("writing BaseIf ready file");
      return EXIT_FAILURE;
    }
  }

  Gateway gateway(&base, listen_fd);
  gateway.Run();
  SimbricksBaseIfClose(&base);
  if (!connect_mode) {
    SimbricksBaseIfSHMPoolUnlink(&pool);
    SimbricksBaseIfSHMPoolUnmap(&pool);
  }
  return EXIT_SUCCESS;
}
