#pragma once

/*
 * Payload carried inside a SimBricks BaseIf upper-layer entry by the LEGOSim
 * PipeComm gateway. All multibyte fields use network byte order. PipeComm
 * reads remain local and blocking at the destination endpoint; only a write's
 * data transfer crosses the BaseIf channel from source to destination.
 */

#include <stdint.h>

enum SimbricksPipeFrameType {
  kSimbricksPipeData = 1,
  kSimbricksPipeError = 2,
};

struct SimbricksPipeFrameHeader {
  uint8_t type;
  uint8_t reserved[3];
  uint64_t request_id;
  uint32_t pipe_name_bytes;
  uint32_t payload_bytes;
} __attribute__((packed));

/* Header is followed by pipe_name_bytes UTF-8 bytes and payload_bytes bytes. */
