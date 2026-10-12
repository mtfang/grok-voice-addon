#pragma once

#ifdef USE_ESP32

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#include "esphome/components/i2s_audio/i2s_audio.h"
#include "esphome/components/microphone/microphone.h"
#include "esphome/core/component.h"
#ifdef USE_SPEAKER
#include "esphome/components/speaker/speaker.h"
#endif
#include <atomic>
#include <vector>
#include <string>

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

namespace esphome {
namespace i2s_audio {

class Sat1RawtapMicrophone : public I2SAudioIn, public microphone::Microphone, public Component {
 public:
  void setup() override;
  void dump_config() override { this->dump_i2s_settings(); }
  void start();
  void stop();

  void loop() override;

  void set_correct_dc_offset(bool correct_dc_offset) { this->correct_dc_offset_ = correct_dc_offset; }

  // ---- rawtap ----
  void set_dump_lines_per_loop(int n) { this->dump_lines_per_loop_ = n; }
#ifdef USE_SPEAKER
  void set_sweep_speaker(speaker::Speaker *spk) { this->sweep_speaker_ = spk; }
#endif
  /// Capture ms of the 7 decoded channels (int16: ch0, ch1, ref, E, W, N, S) into PSRAM, then dump them
  /// as base64 log lines. With sweep a low-level log sweep is played on sweep_speaker starting 1 s in.
  bool start_capture(uint32_t ms, bool sweep, float sweep_dbfs = -30.0f);
  /// (Re)dump the last capture, starting at line from_line.
  bool start_dump(uint32_t from_line = 0);
  void stop_dump() { this->dumping_ = false; }
  bool is_rawtap_locked() const { return this->locked_.load(); }
  std::string tap_status() const;

 protected:
  /// @brief Starts the I2S driver. Updates the ``audio_stream_info_`` member variable with the current setttings.
  /// @return True if succesful, false otherwise
  bool start_driver_();

  /// @brief Stops the I2S driver.
  bool stop_driver_();

  /// @brief Attempts to correct a microphone DC offset; e.g., a microphones silent level is offset from 0. Applies a
  /// correction offset that is updated using an exponential moving average for all samples away from 0.
  /// @param data
  void fix_dc_offset_(std::vector<uint8_t> &data);

  size_t read_(uint8_t *buf, size_t len, TickType_t ticks_to_wait);

  /// @brief Sets the Microphone ``audio_stream_info_`` member variable to the configured I2S settings.
  void configure_stream_settings_();

  static void mic_task(void *params);

  SemaphoreHandle_t active_listeners_semaphore_{nullptr};
  EventGroupHandle_t event_group_{nullptr};
  TaskHandle_t task_handle_{nullptr};
  /// Decodes n_words interleaved 48 kHz L/R words in place into 16 kHz L/R pairs; returns the pair count.
  size_t decode_(int32_t *words, size_t n_words);
  void dump_step_();
  void sweep_step_();

  // decoder state (mic task only, except the atomics)
  std::vector<int32_t> scratch_;
  // sync-loss diagnostics (written by mic task, logged by loop)
  struct LossRec {
    int64_t t_us;
    uint32_t word_idx, n_words, read_gap_us, ovf, short_reads, unlocked_triplets;
    int8_t phase_delta;  // set on relock: (new - old) phase mod 3, -1 = not yet relocked
  };
  static const uint8_t LOSS_RING = 16;
  LossRec loss_ring_[LOSS_RING]{};
  std::atomic<uint32_t> loss_wr_{0};
  uint32_t loss_rd_{0};
  uint8_t phase_before_loss_{0};
  uint32_t unlocked_triplets_{0};
  uint32_t cur_word_idx_{0};
  uint32_t cur_n_words_{0};
  uint32_t cur_read_gap_us_{0};
  uint32_t max_read_gap_us_{0};
  int64_t last_read_us_{0};
  std::atomic<uint32_t> short_reads_{0};
  std::atomic<uint32_t> rx_ovf_{0};
  bool ovf_cb_registered_{false};
 public:
  static bool rx_ovf_cb_(void *handle, void *event, void *user);
 protected:
  int32_t cur_l_{0};
  bool have_l_{false};
  uint8_t k3_{0};
  int32_t tl_[3]{};
  uint32_t tr_[3]{};
  bool cand_ok_[3]{true, true, true};
  uint16_t cand_run_[3]{};
  uint8_t phase_{0};
  uint8_t bad_triplets_{0};
  std::atomic<bool> locked_{false};
  std::atomic<uint32_t> lock_losses_{0};
  std::atomic<uint32_t> lock_events_{0};
  std::atomic<uint32_t> unlocked_samples_{0};
  std::atomic<uint32_t> decoded_samples_{0};

  // capture
  int16_t *cap_buf_{nullptr};
  size_t cap_buf_samples_{0};
  std::atomic<size_t> cap_len_{0};
  std::atomic<size_t> cap_pos_{0};
  std::atomic<bool> capturing_{false};
  std::atomic<uint32_t> cap_unlocked_{0};
  bool cap_reported_{true};
  bool last_locked_{false};
  uint32_t cap_start_ms_{0};
  bool cap_sweep_{false};

  // dump
  bool dumping_{false};
  uint32_t dump_line_{0};
  uint32_t dump_lines_total_{0};
  int dump_lines_per_loop_{4};

  // sweep
#ifdef USE_SPEAKER
  speaker::Speaker *sweep_speaker_{nullptr};
#endif
  int16_t *sweep_buf_{nullptr};
  size_t sweep_len_{0};
  size_t sweep_pos_{0};
  bool sweep_pending_{false};
  bool sweep_playing_{false};
  float sweep_dbfs_{-30.0f};

  bool correct_dc_offset_;
  int32_t dc_offset_{0};
};

}  // namespace i2s_audio
}  // namespace esphome

#endif  // USE_ESP32
