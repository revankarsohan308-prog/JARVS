#include "device_state.h"

namespace devstate {

namespace {
Status _status = BOOTING;
char _detail[96] = "";
}  // namespace

const char* name(Status s) {
  switch (s) {
    case BOOTING:       return "BOOTING";
    case CONNECTING:    return "CONNECTING";
    case READY:         return "READY";
    case LISTENING:     return "LISTENING";
    case THINKING:      return "THINKING";
    case SPEAKING:      return "SPEAKING";
    case OFFLINE:       return "OFFLINE";
    case MIC_MUTED:     return "MIC MUTED";
    case NOT_CONFIGURED:return "NOT CONFIGURED";
    case ERROR:         return "ERROR";
    default:            return "UNKNOWN";
  }
}

void set(Status s, const char* detail) {
  _status = s;
  clearDetail();
  if (detail && detail[0]) {
    strncpy(_detail, detail, sizeof(_detail) - 1);
  }
}

Status get() { return _status; }
const char* detail() { return _detail; }
void clearDetail() { _detail[0] = '\0'; }

}  // namespace devstate