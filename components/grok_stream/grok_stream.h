// grok_stream: full-duplex PCM streaming between an ESPHome voice device and the Grok Voice Live bridge.
// Derived from RealtimeClaw's MIT-licensed wyoming_tcp_client (Copyright (c) 2026 vivu);
// reworked for full duplex, bounded parsing, main-loop-only speaker/mic control, and barge-in (grok-clear).
#pragma once

#ifdef USE_ESP32

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/components/microphone/microphone_source.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/components/ring_buffer/ring_buffer.h"

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <atomic>
#include <memory>
#include <mutex>
#include <deque>
#include <string>

namespace esphome {
namespace grok_stream {

enum class State : uint8_t { IDLE, CONNECTING, ACTIVE, CLOSING };

class GrokStream : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  void set_host(const std::string &h) { this->host_ = h; }
  void set_port(uint16_t p) { this->port_ = p; }
  void set_token(const std::string &t) { this->token_ = t; }
  void set_device_name(const std::string &n) { this->device_name_ = n; }
  void set_microphone_source(microphone::MicrophoneSource *m) { this->mic_source_ = m; }
  void set_speaker(speaker::Speaker *s) { this->speaker_ = s; }
  void set_idle_timeout(uint32_t ms) { this->idle_timeout_ms_ = ms; }
  void set_max_duration(uint32_t ms) { this->max_duration_ms_ = ms; }
  // media player volume (0..1) at wake time, reported in the hello so the bridge can cap Grok's level
  void set_device_volume(float v) { this->device_volume_ = v; }

  void start();
  void stop(const std::string &reason = "stopped");
  bool is_running() const { return this->state_.load() != State::IDLE; }

  Trigger<> *get_start_trigger() { return &this->start_trigger_; }
  Trigger<std::string> *get_end_trigger() { return &this->end_trigger_; }
  Trigger<std::string> *get_state_trigger() { return &this->state_trigger_; }

 protected:
  // ---- network task (all socket I/O happens here) ----
  static void net_task_(void *arg);
  void net_session_();
  bool connect_();
  bool send_all_(const uint8_t *data, size_t len);
  bool send_event_(const char *type, const char *data_json, const uint8_t *payload, size_t payload_len);
  bool pump_rx_();  // non-blocking; returns false on disconnect/protocol error
  void handle_event_(const std::string &type, const std::string &data, const uint8_t *payload, size_t len);
  void post_(const std::string &kind, const std::string &arg = "");

  std::string host_;
  uint16_t port_{10700};
  std::string token_;
  std::string device_name_;
  microphone::MicrophoneSource *mic_source_{nullptr};
  speaker::Speaker *speaker_{nullptr};
  uint32_t idle_timeout_ms_{15000};
  uint32_t max_duration_ms_{130000};

  std::atomic<State> state_{State::IDLE};
  std::atomic<bool> stop_requested_{false};
  int sock_{-1};
  TaskHandle_t net_task_handle_{nullptr};

  std::unique_ptr<ring_buffer::RingBuffer> mic_rb_;  // mic task -> net task
  std::unique_ptr<ring_buffer::RingBuffer> spk_rb_;  // net task -> main loop -> speaker

  // rx parser state (net task only)
  uint8_t *rx_buf_{nullptr};
  size_t rx_len_{0};

  // net task -> main loop messages (speaker/mic control and triggers run on the main loop only)
  std::mutex q_mutex_;
  std::deque<std::pair<std::string, std::string>> q_;
  uint32_t out_rate_{24000};
  bool speaker_active_{false};
  bool want_speaker_{false};
  bool ending_{false};
  bool mic_active_{false};
  uint8_t spk_tmp_[1024];
  size_t pending_len_{0};
  size_t pending_off_{0};
  uint32_t last_rx_ms_{0};
  std::string end_reason_;  // net task only
  std::string stop_reason_req_;  // reason of a local stop() request (guarded by q_mutex_)

  // playback-start markers ("grok-played"): when reply audio starts entering the speaker after an idle speaker,
  // report which reply sample it was and how many mic samples had been captured at that moment, so the bridge
  // can align its echo reference with the mic stream exactly (no wall-clock guessing).
  std::atomic<uint32_t> mic_written_{0};   // mic bytes captured this session (mic task)
  // Wake arbitration: mic level of the last ~2 s (per-callback block RMS, dBFS), so the hello can tell the
  // bridge how loud "Hey Grok" was here; the bridge picks the closest Satellite when several wake at once.
  static constexpr size_t LVL_N = 64;
  float lvl_db_[LVL_N]{};
  std::atomic<uint32_t> lvl_i_{0};
  float wake_rms_db_{-120.0f};
  float device_volume_{-1.0f};
  uint32_t seg_count_{0};                  // audio-start events seen (main loop)
  uint32_t spk_seg_bytes_{0};              // bytes handed to the speaker in this reply (main loop)
  bool spk_idle_{true};                    // speaker ran dry / reply not started (main loop)
  std::mutex marker_mutex_;
  bool marker_ready_{false};
  uint32_t marker_seg_{0}, marker_pos_{0}, marker_mic_{0};
  HighFrequencyLoopRequester high_freq_;

  Trigger<> start_trigger_;
  Trigger<std::string> end_trigger_;
  Trigger<std::string> state_trigger_;
};

template<typename... Ts> class StartAction : public Action<Ts...>, public Parented<GrokStream> {
 public:
  void play(const Ts &...x) override { this->parent_->start(); }
};
template<typename... Ts> class StopAction : public Action<Ts...>, public Parented<GrokStream> {
 public:
  TEMPLATABLE_VALUE(std::string, reason)
  void play(const Ts &...x) override {
    this->parent_->stop(this->reason_.has_value() ? this->reason_.value(x...) : std::string("action"));
  }
};
template<typename... Ts> class IsRunningCondition : public Condition<Ts...>, public Parented<GrokStream> {
 public:
  bool check(const Ts &...x) override { return this->parent_->is_running(); }
};

}  // namespace grok_stream
}  // namespace esphome

#endif  // USE_ESP32
