#include "grok_stream.h"
#include <cmath>
#include <algorithm>

#ifdef USE_ESP32

#include "esphome/core/application.h"
#include "esphome/core/log.h"
#include "esphome/components/audio/audio.h"

#include <lwip/sockets.h>
#include <lwip/netdb.h>
#include <fcntl.h>
#include <errno.h>
#include <cstring>
#include <cstdlib>

namespace esphome {
namespace grok_stream {

static const char *const TAG = "grok_stream";

static constexpr size_t MIC_CHUNK = 1024;            // 32 ms of 16 kHz PCM16 mono
static constexpr size_t MIC_RB_SIZE = 16 * 1024;     // ~0.5 s
static constexpr size_t SPK_RB_SIZE = 96 * 1024;     // ~2 s at 24 kHz (bridge keeps <=0.3 s ahead)
static constexpr size_t MAX_HEADER = 512;
static constexpr size_t MAX_DATA = 1024;
static constexpr size_t MAX_PAYLOAD = 8192;
static constexpr size_t RX_CAP = MAX_HEADER + 1 + MAX_DATA + MAX_PAYLOAD;
static constexpr uint32_t CONNECT_TIMEOUT_MS = 3000;

// ---- tiny bounded JSON helpers (flat objects only) ----
static bool json_find(const std::string &js, const char *key, size_t &val_pos) {
  std::string pat = std::string("\"") + key + "\"";
  size_t p = js.find(pat);
  if (p == std::string::npos) return false;
  p = js.find(':', p + pat.size());
  if (p == std::string::npos) return false;
  p++;
  while (p < js.size() && (js[p] == ' ' || js[p] == '\t')) p++;
  val_pos = p;
  return p < js.size();
}
static long json_int(const std::string &js, const char *key, long def) {
  size_t p;
  if (!json_find(js, key, p)) return def;
  char *end = nullptr;
  long v = strtol(js.c_str() + p, &end, 10);
  return (end == js.c_str() + p) ? def : v;
}
static std::string json_str(const std::string &js, const char *key, size_t max_len = 64) {
  size_t p;
  if (!json_find(js, key, p) || js[p] != '"') return "";
  size_t e = js.find('"', p + 1);
  if (e == std::string::npos) return "";
  return js.substr(p + 1, std::min(e - p - 1, max_len));
}

// ---- lifecycle ----
void GrokStream::setup() {
  this->mic_rb_ = ring_buffer::RingBuffer::create(MIC_RB_SIZE);
  this->spk_rb_ = ring_buffer::RingBuffer::create(SPK_RB_SIZE);
  RAMAllocator<uint8_t> alloc;  // prefers PSRAM
  this->rx_buf_ = alloc.allocate(RX_CAP);
  if (!this->mic_rb_ || !this->spk_rb_ || !this->rx_buf_) {
    ESP_LOGE(TAG, "buffer allocation failed");
    this->mark_failed();
    return;
  }
  this->mic_source_->add_data_callback([this](const std::vector<uint8_t> &data) {
    if (this->state_.load() == State::ACTIVE) {
      this->mic_rb_->write(data.data(), data.size());  // overwrites oldest if the network stalls
      this->mic_written_ += data.size();
    } else if (data.size() >= 2) {
      // idle: keep a short history of mic level (for the wake-arbitration score in the hello)
      const int16_t *x = reinterpret_cast<const int16_t *>(data.data());
      size_t n = data.size() / 2;
      float acc = 0;
      for (size_t i = 0; i < n; i++) acc += float(x[i]) * float(x[i]);
      float db = 10.0f * log10f(acc / n / (32768.0f * 32768.0f) + 1e-12f);
      uint32_t i = this->lvl_i_.load();
      this->lvl_db_[i % LVL_N] = db;
      this->lvl_i_.store(i + 1);
    }
  });
  // Persistent network task (avoids per-session task create/delete); waits for a start notification.
  if (xTaskCreatePinnedToCore(GrokStream::net_task_, "grok_net", 6144, this, 5, &this->net_task_handle_, 1) != pdPASS) {
    ESP_LOGE(TAG, "failed to create network task");
    this->mark_failed();
  }
}

void GrokStream::dump_config() {
  ESP_LOGCONFIG(TAG, "Grok stream:\n  Bridge: %s:%u\n  Token: %s\n  Idle timeout: %u ms\n  Max duration: %u ms",
                this->host_.c_str(), this->port_, this->token_.empty() ? "none" : "set",
                (unsigned) this->idle_timeout_ms_, (unsigned) this->max_duration_ms_);
}

void GrokStream::start() {
  if (this->is_failed()) return;
  State expected = State::IDLE;
  if (!this->state_.compare_exchange_strong(expected, State::CONNECTING)) {
    ESP_LOGW(TAG, "session already active, ignoring start");
    return;
  }
  this->stop_requested_ = false;
  {
    // loudest mic block of the last ~2 s = how loud the wake word was at this device
    float best = -120.0f;
    for (size_t i = 0; i < LVL_N; i++) best = std::max(best, this->lvl_db_[i]);
    this->wake_rms_db_ = best;
  }
  {
    std::lock_guard<std::mutex> lock(this->q_mutex_);
    this->stop_reason_req_.clear();
  }
  this->mic_rb_->reset();
  this->spk_rb_->reset();
  this->mic_written_ = 0;
  this->seg_count_ = 0;
  this->spk_idle_ = true;
  {
    std::lock_guard<std::mutex> lock(this->marker_mutex_);
    this->marker_ready_ = false;
  }
  xTaskNotifyGive(this->net_task_handle_);
}

void GrokStream::stop(const std::string &reason) {
  if (this->state_.load() == State::IDLE) return;
  {
    std::lock_guard<std::mutex> lock(this->q_mutex_);
    this->stop_reason_req_ = reason;
  }
  ESP_LOGI(TAG, "stop requested: %s", reason.c_str());
  this->stop_requested_ = true;
}

void GrokStream::post_(const std::string &kind, const std::string &arg) {
  std::lock_guard<std::mutex> lock(this->q_mutex_);
  if (this->q_.size() < 32) this->q_.emplace_back(kind, arg);
}

// ---- main loop: the only place that touches the speaker, the mic source and triggers ----
void GrokStream::loop() {
  if (!this->mic_active_ && millis() > 15000) {  // after boot/XMOS init settles
    this->mic_source_->start();
    this->mic_active_ = true;
  }
  std::deque<std::pair<std::string, std::string>> events;
  {
    std::lock_guard<std::mutex> lock(this->q_mutex_);
    events.swap(this->q_);
  }
  for (auto &ev : events) {
    const std::string &k = ev.first;
    if (k == "start") {
      this->high_freq_.start();
      this->start_trigger_.trigger();
    } else if (k == "audio-start") {
      this->out_rate_ = std::max(8000L, std::min(48000L, atol(ev.second.c_str())));
      this->want_speaker_ = true;
      this->seg_count_++;
      this->spk_seg_bytes_ = 0;
      this->spk_idle_ = true;
    } else if (k == "clear") {
      this->pending_len_ = 0;
      this->spk_idle_ = true;
      if (this->speaker_active_) {
        this->speaker_->stop();  // drops audio already queued downstream (resampler/mixer)
        this->speaker_active_ = false;
      }
    } else if (k == "state") {
      this->state_trigger_.trigger(ev.second);
    } else if (k == "end") {
      // the mic source stays running between sessions (it feeds the wake-level history; the I2S mic is
      // running anyway for micro_wake_word)
      this->ending_ = true;
      this->want_speaker_ = false;
      this->high_freq_.stop();
      this->end_trigger_.trigger(ev.second);
    }
  }

  // (Re)start the speaker once any previous stop has completed.
  if (this->want_speaker_ && !this->speaker_active_ && this->speaker_->is_stopped()) {
    this->speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 1, this->out_rate_));
    this->speaker_->start();
    this->speaker_active_ = true;
    this->want_speaker_ = false;
  }

  // Feed the speaker without blocking the main loop.
  if (this->speaker_active_) {
    for (int i = 0; i < 8; i++) {
      if (this->pending_len_ == 0) {
        this->pending_len_ = this->spk_rb_->read(this->spk_tmp_, sizeof(this->spk_tmp_), 0) & ~size_t(1);
        this->pending_off_ = 0;
        if (this->pending_len_ == 0) {
          this->spk_idle_ = true;
          break;
        }
      }
      uint32_t mic_now = this->mic_written_.load();
      size_t n = this->speaker_->play(this->spk_tmp_ + this->pending_off_, this->pending_len_, 0);
      if (n > 0 && this->spk_idle_) {
        std::lock_guard<std::mutex> lock(this->marker_mutex_);
        this->marker_seg_ = this->seg_count_;
        this->marker_pos_ = this->spk_seg_bytes_ / 2;  // reply sample index (PCM16 mono at out_rate)
        this->marker_mic_ = mic_now / 2;               // mic sample index (PCM16 mono 16 kHz)
        this->marker_ready_ = true;
        this->spk_idle_ = false;
      }
      this->spk_seg_bytes_ += n;
      this->pending_off_ += n;
      this->pending_len_ -= n;
      if (this->pending_len_ > 0) break;  // speaker buffer full, retry next loop
    }
    if (this->ending_ && this->pending_len_ == 0 && this->spk_rb_->available() == 0) {
      this->speaker_->finish();  // let the tail play out, then stop
      this->speaker_active_ = false;
      this->ending_ = false;
    }
  } else if (this->ending_ && !this->want_speaker_) {
    this->ending_ = false;
  }
}

// ---- network task ----
void GrokStream::net_task_(void *arg) {
  auto *self = static_cast<GrokStream *>(arg);
  while (true) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    self->net_session_();
  }
}

bool GrokStream::connect_() {
  struct sockaddr_in addr {};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(this->port_);
  if (::inet_pton(AF_INET, this->host_.c_str(), &addr.sin_addr) != 1) {
    struct addrinfo hints {}, *res = nullptr;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    if (::getaddrinfo(this->host_.c_str(), nullptr, &hints, &res) != 0 || res == nullptr) {
      ESP_LOGE(TAG, "cannot resolve %s", this->host_.c_str());
      return false;
    }
    addr.sin_addr = reinterpret_cast<struct sockaddr_in *>(res->ai_addr)->sin_addr;
    ::freeaddrinfo(res);
  }
  int s = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (s < 0) return false;
  int flags = ::fcntl(s, F_GETFL, 0);
  ::fcntl(s, F_SETFL, flags | O_NONBLOCK);
  int r = ::connect(s, reinterpret_cast<struct sockaddr *>(&addr), sizeof(addr));
  if (r != 0 && errno != EINPROGRESS) {
    ::close(s);
    return false;
  }
  if (r != 0) {
    fd_set wfds;
    FD_ZERO(&wfds);
    FD_SET(s, &wfds);
    struct timeval tv { (long) (CONNECT_TIMEOUT_MS / 1000), (long) ((CONNECT_TIMEOUT_MS % 1000) * 1000) };
    int err = 0;
    socklen_t len = sizeof(err);
    if (::select(s + 1, nullptr, &wfds, nullptr, &tv) <= 0 || ::getsockopt(s, SOL_SOCKET, SO_ERROR, &err, &len) != 0 ||
        err != 0) {
      ESP_LOGE(TAG, "connect to %s:%u failed/timed out", this->host_.c_str(), this->port_);
      ::close(s);
      return false;
    }
  }
  int one = 1;
  ::setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  this->sock_ = s;
  return true;
}

bool GrokStream::send_all_(const uint8_t *data, size_t len) {
  uint32_t start = millis();
  size_t sent = 0;
  while (sent < len) {
    ssize_t n = ::send(this->sock_, data + sent, len - sent, MSG_DONTWAIT);
    if (n > 0) {
      sent += n;
      continue;
    }
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
      if (millis() - start > 1000) {
        ESP_LOGW(TAG, "send stalled for 1 s");
        return false;
      }
      vTaskDelay(pdMS_TO_TICKS(2));
      continue;
    }
    return false;
  }
  return true;
}

bool GrokStream::send_event_(const char *type, const char *data_json, const uint8_t *payload, size_t payload_len) {
  char header[160];
  size_t dlen = data_json ? strlen(data_json) : 0;
  int hl = snprintf(header, sizeof(header), "{\"type\":\"%s\",\"version\":\"1.5.0\",\"data_length\":%u,\"payload_length\":%u}\n",
                    type, (unsigned) dlen, (unsigned) payload_len);
  if (hl <= 0 || hl >= (int) sizeof(header)) return false;
  if (!this->send_all_(reinterpret_cast<uint8_t *>(header), hl)) return false;
  if (dlen && !this->send_all_(reinterpret_cast<const uint8_t *>(data_json), dlen)) return false;
  if (payload_len && !this->send_all_(payload, payload_len)) return false;
  return true;
}

bool GrokStream::pump_rx_() {
  // read whatever is available
  while (this->rx_len_ < RX_CAP) {
    ssize_t n = ::recv(this->sock_, this->rx_buf_ + this->rx_len_, RX_CAP - this->rx_len_, MSG_DONTWAIT);
    if (n > 0) {
      this->rx_len_ += n;
      this->last_rx_ms_ = millis();
      continue;
    }
    if (n == 0) return false;  // peer closed
    if (errno == EAGAIN || errno == EWOULDBLOCK) break;
    return false;
  }
  // parse complete events
  while (this->rx_len_ > 0) {
    uint8_t *nl = static_cast<uint8_t *>(memchr(this->rx_buf_, '\n', std::min(this->rx_len_, MAX_HEADER + 1)));
    if (nl == nullptr) {
      if (this->rx_len_ > MAX_HEADER) {
        ESP_LOGE(TAG, "header too long");
        return false;
      }
      return true;  // need more bytes
    }
    size_t hlen = nl - this->rx_buf_;
    std::string header(reinterpret_cast<char *>(this->rx_buf_), hlen);
    long dlen = json_int(header, "data_length", 0);
    long plen = json_int(header, "payload_length", 0);
    if (dlen < 0 || plen < 0 || (size_t) dlen > MAX_DATA || (size_t) plen > MAX_PAYLOAD) {
      ESP_LOGE(TAG, "event length out of range");
      return false;
    }
    size_t total = hlen + 1 + dlen + plen;
    if (this->rx_len_ < total) return true;  // wait for the rest
    std::string type = json_str(header, "type", 32);
    std::string data(reinterpret_cast<char *>(this->rx_buf_ + hlen + 1), dlen);
    this->handle_event_(type, data, this->rx_buf_ + hlen + 1 + dlen, plen);
    memmove(this->rx_buf_, this->rx_buf_ + total, this->rx_len_ - total);
    this->rx_len_ -= total;
  }
  return true;
}

void GrokStream::handle_event_(const std::string &type, const std::string &data, const uint8_t *payload, size_t len) {
  if (type == "audio-chunk") {
    if (len > 0) {
      size_t w = this->spk_rb_->write_without_replacement(payload, len, 0);
      if (w < len) ESP_LOGW(TAG, "speaker buffer full, dropped %u bytes", (unsigned) (len - w));
    }
  } else if (type == "audio-start") {
    this->post_("audio-start", std::to_string(json_int(data, "rate", 24000)));
  } else if (type == "audio-stop") {
    // end of one reply; the speaker keeps running for the rest of the session
  } else if (type == "grok-clear") {
    this->spk_rb_->reset();  // barge-in: drop unplayed reply audio immediately
    this->post_("clear");
  } else if (type == "grok-state") {
    this->post_("state", json_str(data, "state", 24));
  } else if (type == "grok-stand-down") {
    // another Satellite answered this wake word: stop quietly and go back to wake listening (no error)
    ESP_LOGI(TAG, "standing down: %s", json_str(data, "reason", 32).c_str());
    this->end_reason_ = "stand_down";
    this->stop_requested_ = true;
  } else if (type == "grok-end") {
    this->end_reason_ = json_str(data, "reason", 24);
    this->stop_requested_ = true;
  } else if (type == "transcript") {
    ESP_LOGI(TAG, "%s: %s", json_str(data, "role", 16).c_str(), json_str(data, "text", 200).c_str());
  } else {
    ESP_LOGD(TAG, "ignoring event '%s'", type.c_str());
  }
}

void GrokStream::net_session_() {
  this->end_reason_.clear();
  this->rx_len_ = 0;
  if (!this->connect_()) {
    this->state_ = State::IDLE;
    this->post_("end", "connect_failed");
    return;
  }
  char hello[224];
  snprintf(hello, sizeof(hello), "{\"token\":\"%s\",\"device\":\"%s\",\"version\":2,\"wake_rms_db\":%.1f}",
           this->token_.c_str(), this->device_name_.c_str(), this->wake_rms_db_);
  const char *fmt = "{\"rate\":16000,\"width\":2,\"channels\":1}";
  bool ok = this->send_event_("grok-hello", hello, nullptr, 0) && this->send_event_("audio-start", fmt, nullptr, 0);
  ESP_LOGI(TAG, "connected to %s:%u, streaming", this->host_.c_str(), this->port_);
  this->state_ = State::ACTIVE;
  this->post_("start");

  const uint32_t t0 = millis();
  this->last_rx_ms_ = t0;
  std::string reason = "stopped";
  uint8_t chunk[MIC_CHUNK];
  while (ok) {
    if (this->stop_requested_) {
      if (!this->end_reason_.empty()) {
        reason = this->end_reason_;  // the bridge ended it (its audio-stop carried the reason)
      } else {
        std::lock_guard<std::mutex> lock(this->q_mutex_);
        reason = this->stop_reason_req_.empty() ? "stopped" : this->stop_reason_req_;
      }
      break;
    }
    bool did_work = false;
    {
      char mk[96];
      bool send_mk = false;
      {
        std::lock_guard<std::mutex> lock(this->marker_mutex_);
        if (this->marker_ready_) {
          snprintf(mk, sizeof(mk), "{\"seg\":%u,\"pos\":%u,\"mic\":%u}", (unsigned) this->marker_seg_,
                   (unsigned) this->marker_pos_, (unsigned) this->marker_mic_);
          this->marker_ready_ = false;
          send_mk = true;
        }
      }
      if (send_mk && !this->send_event_("grok-played", mk, nullptr, 0)) {
        reason = "send_failed";
        break;
      }
    }
    while (this->mic_rb_->available() >= MIC_CHUNK) {
      this->mic_rb_->read(chunk, MIC_CHUNK, 0);
      if (!this->send_event_("audio-chunk", fmt, chunk, MIC_CHUNK)) {
        ok = false;
        reason = "send_failed";
        break;
      }
      did_work = true;
    }
    if (!ok) break;
    size_t before = this->rx_len_;
    uint32_t rx_before = this->last_rx_ms_;
    if (!this->pump_rx_()) {
      reason = "disconnected";
      break;
    }
    did_work |= (this->last_rx_ms_ != rx_before) || (this->rx_len_ != before);
    uint32_t now = millis();
    if (now - this->last_rx_ms_ > this->idle_timeout_ms_) {
      reason = "device_idle_timeout";
      break;
    }
    if (now - t0 > this->max_duration_ms_) {
      reason = "device_max_duration";
      break;
    }
    if (!did_work) vTaskDelay(pdMS_TO_TICKS(5));
  }

  this->state_ = State::CLOSING;
  {
    // tell the bridge WHY the device ended the session (best effort)
    std::string r;
    for (char c : reason) r += (c == '"' || c == '\\' || (unsigned char) c < 0x20) ? '_' : c;
    std::string js = "{\"reason\":\"" + r + "\"}";
    this->send_event_("audio-stop", js.c_str(), nullptr, 0);
  }
  ::shutdown(this->sock_, SHUT_RDWR);
  ::close(this->sock_);
  this->sock_ = -1;
  ESP_LOGI(TAG, "session ended: %s (%.1f s)", reason.c_str(), (millis() - t0) / 1000.0f);
  this->post_("end", reason);
  this->state_ = State::IDLE;
}

}  // namespace grok_stream
}  // namespace esphome

#endif  // USE_ESP32
