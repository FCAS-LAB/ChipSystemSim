#define _POSIX_C_SOURCE 200809L

/*
 * Minimal external SimBricks BaseIf endpoint used to validate the transport
 * that will carry LEGOSim PipeComm frames. It deliberately uses the official
 * BaseIf queue API; net_sockets transports this endpoint's queue to its peer.
 *
 * Usage: simbricks_base_probe [--connect] SOCKET SHM_PATH [PAYLOAD]
 *
 * Start one probe per host. Each probe listens on SOCKET for a local
 * net_sockets -C connection. The optional payload is sent as an upper-layer
 * BaseIf message and every received payload is written to stdout.
 */

#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <simbricks/base/if.h>

enum {
  kEntryBytes = 65536,
  kEntryCount = 128,
  kPayloadOffset = sizeof(struct SimbricksProtoBaseMsgHeader),
  kMessageType = SIMBRICKS_PROTO_MSG_TYPE_UPPER_START,
};

static volatile sig_atomic_t stopping;

static void HandleSignal(int signal_number) {
  (void)signal_number;
  stopping = 1;
}

static int SendPayload(struct SimbricksBaseIf *base, const uint8_t *payload,
                       uint32_t length) {
  if (length > kEntryBytes - kPayloadOffset - sizeof(length)) {
    fprintf(stderr, "payload is too large for one BaseIf entry: %u\n", length);
    return -1;
  }

  volatile union SimbricksProtoBaseMsg *message =
      SimbricksBaseIfOutAlloc(base, 0);
  if (message == NULL) {
    fprintf(stderr, "BaseIf output queue is full\n");
    return -1;
  }

  uint8_t *body = (uint8_t *)(void *)message + kPayloadOffset;
  memcpy(body, &length, sizeof(length));
  memcpy(body + sizeof(length), payload, length);
  SimbricksBaseIfOutSend(base, message, kMessageType);
  return 0;
}

static int ReceivePayload(struct SimbricksBaseIf *base) {
  volatile union SimbricksProtoBaseMsg *message = SimbricksBaseIfInPeek(base, 0);
  if (message == NULL) return 0;

  uint8_t type = SimbricksBaseIfInType(base, message);
  if (type == SIMBRICKS_PROTO_MSG_TYPE_TERMINATE) {
    SimbricksBaseIfInDone(base, message);
    return 1;
  }
  if (type != kMessageType) {
    fprintf(stderr, "unexpected BaseIf message type: %u\n", type);
    SimbricksBaseIfInDone(base, message);
    return -1;
  }

  const uint8_t *body = (const uint8_t *)(const void *)message + kPayloadOffset;
  uint32_t length;
  memcpy(&length, body, sizeof(length));
  if (length > kEntryBytes - kPayloadOffset - sizeof(length)) {
    fprintf(stderr, "invalid incoming payload length: %u\n", length);
    SimbricksBaseIfInDone(base, message);
    return -1;
  }
  if (fwrite(body + sizeof(length), 1, length, stdout) != length ||
      fputc('\n', stdout) == EOF) {
    perror("writing received payload");
    SimbricksBaseIfInDone(base, message);
    return -1;
  }
  fflush(stdout);
  SimbricksBaseIfInDone(base, message);
  return 0;
}

int main(int argc, char **argv) {
  int argument = 1;
  bool connect_mode = false;
  if (argument < argc && strcmp(argv[argument], "--connect") == 0) {
    connect_mode = true;
    argument++;
  }
  if (argc - argument < 2 || argc - argument > 3) {
    fprintf(stderr, "Usage: %s [--connect] SOCKET SHM_PATH [PAYLOAD]\n", argv[0]);
    return EXIT_FAILURE;
  }

  signal(SIGINT, HandleSignal);
  signal(SIGTERM, HandleSignal);

  struct SimbricksBaseIfParams params;
  SimbricksBaseIfDefaultParams(&params);
  params.sock_path = argv[argument];
  params.blocking_conn = true;
  params.sync_mode = kSimbricksBaseIfSyncDisabled;
  params.in_entries_size = kEntryBytes;
  params.out_entries_size = kEntryBytes;
  params.in_num_entries = kEntryCount;
  params.out_num_entries = kEntryCount;

  struct SimbricksBaseIf base;
  struct SimbricksBaseIfSHMPool pool = {0};
  if (SimbricksBaseIfInit(&base, &params)) {
    perror("initializing BaseIf");
    return EXIT_FAILURE;
  }
  if (connect_mode) {
    struct SimbricksBaseIf *interfaces[] = {&base};
    if (SimbricksBaseIfConnect(&base) || SimbricksBaseIfConnsWait(interfaces, 1)) {
      perror("connecting BaseIf socket");
      return EXIT_FAILURE;
    }
  } else {
    if (SimbricksBaseIfSHMPoolCreate(&pool, argv[argument + 1],
                                     SimbricksBaseIfSHMSize(&params)) ||
        SimbricksBaseIfListen(&base, &pool)) {
      perror("listening on BaseIf socket");
      SimbricksBaseIfSHMPoolUnlink(&pool);
      SimbricksBaseIfSHMPoolUnmap(&pool);
      return EXIT_FAILURE;
    }
  }
  if (SimbricksBaseIfIntroSend(&base, NULL, 0)) {
    perror("sending BaseIf intro");
    return EXIT_FAILURE;
  }
  uint8_t intro[1];
  size_t intro_length = sizeof(intro);
  if (SimbricksBaseIfIntroRecv(&base, intro, &intro_length)) {
    perror("receiving BaseIf intro");
    return EXIT_FAILURE;
  }

  if (argc - argument == 3 && SendPayload(&base, (const uint8_t *)argv[argument + 2],
                                          (uint32_t)strlen(argv[argument + 2]))) {
    return EXIT_FAILURE;
  }

  while (!stopping && !SimbricksBaseIfInTerminated(&base)) {
    int result = ReceivePayload(&base);
    if (result != 0) break;
    struct timespec pause = {.tv_sec = 0, .tv_nsec = 1000000};
    nanosleep(&pause, NULL);
  }

  SimbricksBaseIfClose(&base);
  if (!connect_mode) {
    SimbricksBaseIfSHMPoolUnlink(&pool);
    SimbricksBaseIfSHMPoolUnmap(&pool);
  }
  return EXIT_SUCCESS;
}
