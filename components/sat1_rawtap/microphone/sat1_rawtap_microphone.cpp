#include "sat1_rawtap_microphone.h"

#ifdef USE_ESP32

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <esp_heap_caps.h>
#include <esp_rom_crc.h>
#include <esp_timer.h>
#include <driver/i2s_std.h>

namespace esphome {
namespace i2s_audio {

static const size_t RING_BUFFER_LENGTH = 60;  // Measured in milliseconds
static const size_t QUEUE_LENGTH = 10;

static const UBaseType_t MAX_LISTENERS = 16;

static const uint32_t READ_DURATION_MS = 16;

static const size_t TASK_STACK_SIZE = 4096;
static const ssize_t TASK_PRIORITY = 17;

// Use an exponential moving average to correct a DC offset with weight factor 1/1000
static const int32_t DC_OFFSET_MOVING_AVERAGE_COEFFICIENT_DENOMINATOR = 1000;

static const char *const TAG = "i2s_audio.sat1_microphone";
static const char *const RT_TAG = "rawtap";

enum MicrophoneEventGroupBits : uint32_t {
  COMMAND_STOP = (1 << 0),  // stops the microphone task, set and cleared by ``loop``

  TASK_STARTING = (1 << 10),  // set by mic task, cleared by ``loop``
  TASK_RUNNING = (1 << 11),   // set by mic task, cleared by ``loop``
  TASK_STOPPED = (1 << 13),   // set by mic task, cleared by ``loop``

  ALL_BITS = 0x00FFFFFF,  // All valid FreeRTOS event group bits
};

void Sat1RawtapMicrophone::setup() {
  ESP_LOGCONFIG(TAG, "Setting up (SAT1) I2S Audio Microphone...");
  if (this->pdm_) {
    ESP_LOGE(TAG, "PDM not supported for SAT1 integration!");
    this->mark_failed();
    return;
  }

  this->active_listeners_semaphore_ = xSemaphoreCreateCounting(MAX_LISTENERS, MAX_LISTENERS);
  if (this->active_listeners_semaphore_ == nullptr) {
    ESP_LOGE(TAG, "Failed to create semaphore");
    this->mark_failed();
    return;
  }

  this->event_group_ = xEventGroupCreate();
  if (this->event_group_ == nullptr) {
    ESP_LOGE(TAG, "Failed to create event group");
    this->mark_failed();
    return;
  }

  this->configure_stream_settings_();
}

void Sat1RawtapMicrophone::start() {
  if (this->is_failed())
    return;

  xSemaphoreTake(this->active_listeners_semaphore_, 0);
}

void Sat1RawtapMicrophone::stop() {
  if (this->state_ == microphone::STATE_STOPPED || this->is_failed())
    return;

  xSemaphoreGive(this->active_listeners_semaphore_);
}

void Sat1RawtapMicrophone::loop() {
  uint32_t event_group_bits = xEventGroupGetBits(this->event_group_);

  if (event_group_bits & MicrophoneEventGroupBits::TASK_STARTING) {
    ESP_LOGD(TAG, "Task started, attempting to allocate buffer");
    xEventGroupClearBits(this->event_group_, MicrophoneEventGroupBits::TASK_STARTING);
  }

  if (event_group_bits & MicrophoneEventGroupBits::TASK_RUNNING) {
    ESP_LOGD(TAG, "Task is running and reading data");

    xEventGroupClearBits(this->event_group_, MicrophoneEventGroupBits::TASK_RUNNING);
    this->state_ = microphone::STATE_RUNNING;
  }

  if ((event_group_bits & MicrophoneEventGroupBits::TASK_STOPPED)) {
    ESP_LOGD(TAG, "Task finished, freeing resources and uninstalling I2S driver");

    vTaskDelete(this->task_handle_);
    this->task_handle_ = nullptr;
    this->stop_driver_();
    xEventGroupClearBits(this->event_group_, ALL_BITS);
    this->status_clear_error();

    this->state_ = microphone::STATE_STOPPED;
  }

  // Start the microphone if any semaphores are taken
  if ((uxSemaphoreGetCount(this->active_listeners_semaphore_) < MAX_LISTENERS) &&
      (this->state_ == microphone::STATE_STOPPED)) {
    this->state_ = microphone::STATE_STARTING;
  }

  // Stop the microphone if all semaphores are returned
  if ((uxSemaphoreGetCount(this->active_listeners_semaphore_) == MAX_LISTENERS) &&
      (this->state_ == microphone::STATE_RUNNING)) {
    this->state_ = microphone::STATE_STOPPING;
  }

  switch (this->state_) {
    case microphone::STATE_STARTING:
      if (this->status_has_error()) {
        break;
      }

      if (!this->start_driver_()) {
        this->status_momentary_error("I2S driver failed to start, unloading it and attempting again in 1 second", 1000);
        this->stop_driver_();  // Stop/frees whatever possibly started
        break;
      }

      if (this->task_handle_ == nullptr) {
        xTaskCreate(Sat1RawtapMicrophone::mic_task, "mic_task", TASK_STACK_SIZE, (void *) this, TASK_PRIORITY,
                    &this->task_handle_);

        if (this->task_handle_ == nullptr) {
          this->status_momentary_error("Task failed to start, attempting again in 1 second", 1000);
          this->stop_driver_();  // Stops the driver to return the lock; will be reloaded in next attempt
        }
      }

      break;
    case microphone::STATE_RUNNING:
      break;
    case microphone::STATE_STOPPING:
      xEventGroupSetBits(this->event_group_, MicrophoneEventGroupBits::COMMAND_STOP);
      break;
    case microphone::STATE_STOPPED:
      break;
  }

  // ---- rawtap housekeeping (main loop) ----
  bool lk = this->locked_.load();
  if (lk != this->last_locked_) {
    this->last_locked_ = lk;
    ESP_LOGI(RT_TAG, "rawtap sync %s (locks=%u losses=%u)", lk ? "LOCKED" : "lost/unlocked",
             (unsigned) this->lock_events_.load(), (unsigned) this->lock_losses_.load());
  }
  if (!this->cap_reported_ && !this->capturing_.load()) {
    this->cap_reported_ = true;
    ESP_LOGI(RT_TAG, "capture done: %u samples, %u unlocked", (unsigned) this->cap_pos_.load(),
             (unsigned) this->cap_unlocked_.load());
    this->start_dump(0);
  }
  // sync-loss diagnostics: log each record once its relock is known (or after 1 s)
  while (this->loss_rd_ < this->loss_wr_.load()) {
    if (this->loss_wr_.load() - this->loss_rd_ > LOSS_RING) this->loss_rd_ = this->loss_wr_.load() - LOSS_RING;
    LossRec lr = this->loss_ring_[this->loss_rd_ % LOSS_RING];
    if (lr.phase_delta < 0 && esp_timer_get_time() - lr.t_us < 1000000) break;
    ESP_LOGW(RT_TAG, "sync loss #%u: word %u/%u of read, read gap %u us (max %u), rx_dma_ovf %u%s, short reads %u, "
             "relock phase delta %d after %u triplets",
             (unsigned) (this->loss_rd_ + 1), (unsigned) lr.word_idx, (unsigned) lr.n_words, (unsigned) lr.read_gap_us,
             (unsigned) this->max_read_gap_us_, (unsigned) lr.ovf, this->ovf_cb_registered_ ? "" : " (cb n/a)",
             (unsigned) lr.short_reads, (int) lr.phase_delta, (unsigned) lr.unlocked_triplets);
    this->loss_rd_++;
  }
  this->sweep_step_();
  this->dump_step_();
}

void Sat1RawtapMicrophone::configure_stream_settings_() {
  uint8_t channel_count = this->num_of_channels();
  uint8_t bits_per_sample = 32;
  if (this->slot_bit_width_ != I2S_SLOT_BIT_WIDTH_AUTO) {
    bits_per_sample = this->slot_bit_width_;
  }

  if (this->slot_mode_ == I2S_SLOT_MODE_STEREO) {
    channel_count = 2;
  }
  // report 16kHz sample rate, as the 48kHz i2s samples will be subsampled to 16kHz
  this->audio_stream_info_ = audio::AudioStreamInfo(bits_per_sample, channel_count, 16000);
}

bool Sat1RawtapMicrophone::rx_ovf_cb_(void *handle, void *event, void *user) {
  ((Sat1RawtapMicrophone *) user)->rx_ovf_++;
  return false;
}

bool Sat1RawtapMicrophone::start_driver_() {
  if (!this->start_i2s_channel_()) {
    ESP_LOGE(TAG, "Failed to start I2S channel");
    return false;
  }
  this->last_read_us_ = 0;
  {
    // rawtap diagnostics: count RX DMA queue overflows (callbacks can only be registered while disabled)
    i2s_chan_handle_t rx = this->parent_->get_rx_handle();
    i2s_event_callbacks_t cbs = {};
    cbs.on_recv_q_ovf = (i2s_isr_callback_t) &Sat1RawtapMicrophone::rx_ovf_cb_;
    esp_err_t e1 = i2s_channel_disable(rx);
    esp_err_t e2 = i2s_channel_register_event_callback(rx, &cbs, this);
    esp_err_t e3 = i2s_channel_enable(rx);
    this->ovf_cb_registered_ = (e2 == ESP_OK);
    if (e1 != ESP_OK || e2 != ESP_OK || e3 != ESP_OK)
      ESP_LOGW(RT_TAG, "RX ovf callback: disable=%s register=%s enable=%s", esp_err_to_name(e1), esp_err_to_name(e2),
               esp_err_to_name(e3));
  }
  this->configure_stream_settings_();  // redetermine the settings in case some settings were changed after compilation
  return true;
}

bool Sat1RawtapMicrophone::stop_driver_() { return this->stop_i2s_channel_(); }

size_t Sat1RawtapMicrophone::read_(uint8_t *buf, size_t len, TickType_t ticks_to_wait) {
  size_t bytes_read = 0;
  // i2s_channel_read expects the timeout value in ms, not ticks
  esp_err_t err = i2s_channel_read(this->parent_->get_rx_handle(), buf, len, &bytes_read, pdTICKS_TO_MS(ticks_to_wait));
  if ((err != ESP_OK) && ((err != ESP_ERR_TIMEOUT) || (ticks_to_wait != 0))) {
    // Ignore ESP_ERR_TIMEOUT if ticks_to_wait = 0, as it will read the data on the next call
    if (!this->status_has_warning()) {
      // Avoid spamming the logs with the error message if its repeated
      ESP_LOGW(TAG, "Error reading from I2S microphone: %s", esp_err_to_name(err));
    }
    this->status_set_warning();
    return 0;
  }
  if ((bytes_read == 0) && (ticks_to_wait > 0)) {
    this->status_set_warning();
    return 0;
  }
  this->status_clear_warning();

  return bytes_read;
}

void Sat1RawtapMicrophone::fix_dc_offset_(std::vector<uint8_t> &data) {
  const size_t bytes_per_sample = this->audio_stream_info_.samples_to_bytes(1);
  const uint32_t total_samples = this->audio_stream_info_.bytes_to_samples(data.size());

  if (total_samples == 0) {
    return;
  }

  int64_t offset_accumulator = 0;
  for (uint32_t sample_index = 0; sample_index < total_samples; ++sample_index) {
    const uint32_t byte_index = sample_index * bytes_per_sample;
    int32_t sample = audio::unpack_audio_sample_to_q31(&data[byte_index], bytes_per_sample);
    offset_accumulator += sample;
    sample -= this->dc_offset_;
    audio::pack_q31_as_audio_sample(sample, &data[byte_index], bytes_per_sample);
  }

  const int32_t new_offset = offset_accumulator / total_samples;
  this->dc_offset_ = new_offset / DC_OFFSET_MOVING_AVERAGE_COEFFICIENT_DENOMINATOR +
                     (DC_OFFSET_MOVING_AVERAGE_COEFFICIENT_DENOMINATOR - 1) * this->dc_offset_ /
                         DC_OFFSET_MOVING_AVERAGE_COEFFICIENT_DENOMINATOR;
}

void Sat1RawtapMicrophone::mic_task(void *params) {
  Sat1RawtapMicrophone *this_microphone = (Sat1RawtapMicrophone *) params;
  xEventGroupSetBits(this_microphone->event_group_, MicrophoneEventGroupBits::TASK_STARTING);

  {  // Ensures the samples vector is freed when the task stops
    // read 3 times the amount of bytes as we need to subsample from 48 kHz to 16 kHz
    const size_t bytes_to_read = 3 * this_microphone->audio_stream_info_.ms_to_bytes(READ_DURATION_MS);
    std::vector<uint8_t> samples;
    samples.reserve(bytes_to_read);

    xEventGroupSetBits(this_microphone->event_group_, MicrophoneEventGroupBits::TASK_RUNNING);
    while (!(xEventGroupGetBits(this_microphone->event_group_) & MicrophoneEventGroupBits::COMMAND_STOP)) {
      if (this_microphone->data_callbacks_.size() > 0) {
        samples.resize(bytes_to_read);
        size_t bytes_read = this_microphone->read_(samples.data(), bytes_to_read, 2 * pdMS_TO_TICKS(READ_DURATION_MS));
        {
          int64_t now = esp_timer_get_time();
          if (this_microphone->last_read_us_ != 0) {
            uint32_t gap = (uint32_t) (now - this_microphone->last_read_us_);
            this_microphone->cur_read_gap_us_ = gap;
            if (gap > this_microphone->max_read_gap_us_) this_microphone->max_read_gap_us_ = gap;
          }
          this_microphone->last_read_us_ = now;
          if (bytes_read < bytes_to_read) this_microphone->short_reads_++;
        }
        size_t samples_read = bytes_read / sizeof(int32_t);
        int32_t *samples_32 = reinterpret_cast<int32_t *>(samples.data());
        size_t pairs = this_microphone->decode_(samples_32, samples_read);
        samples.resize(pairs * 2 * sizeof(int32_t));
        if (this_microphone->correct_dc_offset_) {
          this_microphone->fix_dc_offset_(samples);
        }
        this_microphone->data_callbacks_.call(samples);
      } else {
        vTaskDelay(pdMS_TO_TICKS(READ_DURATION_MS));
      }
    }
  }

  xEventGroupSetBits(this_microphone->event_group_, MicrophoneEventGroupBits::TASK_STOPPED);
  while (true) {
    // Continuously delay until the loop method deletes the task
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}


// ============================== rawtap ==============================
// XMOS rawtap packing, per 16 kHz sample n (48 kHz stereo frames 3n..3n+2):
//   L[3n+k] = ch0 (AEC+IC+NS+AGC), repeated 3x
//   R[3n+0] = ch1[31:16] | ref[31:17]<<1 | 1     (marker LSB = 1)
//   R[3n+1] = E[31:16]   | W[31:17]<<1
//   R[3n+2] = N[31:16]   | S[31:17]<<1
static const uint16_t RAWTAP_LOCK_TRIPLETS = 32;
static const uint8_t RAWTAP_UNLOCK_BAD_TRIPLETS = 2;
static const size_t RAWTAP_CH = 7;
static const size_t RAWTAP_SAMPLES_PER_LINE = 12;  // 12 * 7 * 2 = 168 bytes -> 224 base64 chars

size_t Sat1RawtapMicrophone::decode_(int32_t *words, size_t n_words) {
  // decode from a copy so in-place output can never overtake unread input (reads may split an L/R frame)
  if (this->scratch_.size() < n_words)
    this->scratch_.resize(n_words);
  memcpy(this->scratch_.data(), words, n_words * sizeof(int32_t));
  const int32_t *in = this->scratch_.data();
  size_t out = 0;
  for (size_t i = 0; i < n_words; i++) {
    if (!this->have_l_) {
      this->cur_l_ = in[i];
      this->have_l_ = true;
      continue;
    }
    this->have_l_ = false;
    const int32_t l = this->cur_l_;
    const uint32_t r = (uint32_t) in[i];
    const uint8_t k = this->k3_;
    this->k3_ = (k == 2) ? 0 : k + 1;
    this->tl_[k] = l;
    this->tr_[k] = r;

    // phase candidates: candidate p means frames with k == p carry the marker word
    const bool lsb = (r & 1u) != 0;
    for (uint8_t p = 0; p < 3; p++) {
      if (lsb != (k == p))
        this->cand_ok_[p] = false;
      if (k == (uint8_t) ((p + 2) % 3)) {  // end of a triplet for this candidate
        if (this->cand_ok_[p]) {
          if (this->cand_run_[p] < 60000)
            this->cand_run_[p]++;
        } else {
          this->cand_run_[p] = 0;
        }
        this->cand_ok_[p] = true;
      }
    }

    if (this->locked_.load(std::memory_order_relaxed)) {
      const uint8_t p = this->phase_;
      if (k != (uint8_t) ((p + 2) % 3))
        continue;  // emit one 16 kHz sample per triplet, at its last frame
      if (this->cand_run_[p] == 0) {
        if (++this->bad_triplets_ >= RAWTAP_UNLOCK_BAD_TRIPLETS) {
          this->locked_.store(false);
          this->lock_losses_++;
          this->bad_triplets_ = 0;
          this->phase_before_loss_ = p;
          this->unlocked_triplets_ = 0;
          uint32_t w = this->loss_wr_.load();
          LossRec &lr = this->loss_ring_[w % LOSS_RING];
          lr.t_us = esp_timer_get_time();
          lr.word_idx = (uint32_t) i;
          lr.n_words = (uint32_t) n_words;
          lr.read_gap_us = this->cur_read_gap_us_;
          lr.ovf = this->rx_ovf_.load();
          lr.short_reads = this->short_reads_.load();
          lr.unlocked_triplets = 0;
          lr.phase_delta = -1;
          this->loss_wr_.store(w + 1);
        }
      } else {
        this->bad_triplets_ = 0;
      }
      const uint32_t r0 = this->tr_[p], r1 = this->tr_[(p + 1) % 3], r2 = this->tr_[(p + 2) % 3];
      const int32_t ch0 = this->tl_[p];
      const int32_t ch1 = (int32_t) (r0 & 0xFFFF0000u);
      words[2 * out] = ch0;
      words[2 * out + 1] = ch1;
      out++;
      this->decoded_samples_++;
      if (this->capturing_.load(std::memory_order_relaxed)) {
        size_t pos = this->cap_pos_.load(std::memory_order_relaxed);
        if (pos < this->cap_len_.load(std::memory_order_relaxed)) {
          int16_t *d = this->cap_buf_ + pos * RAWTAP_CH;
          d[0] = (int16_t) (ch0 >> 16);
          d[1] = (int16_t) (r0 >> 16);
          d[2] = (int16_t) (r0 & 0xFFFEu);
          d[3] = (int16_t) (r1 >> 16);
          d[4] = (int16_t) (r1 & 0xFFFEu);
          d[5] = (int16_t) (r2 >> 16);
          d[6] = (int16_t) (r2 & 0xFFFEu);
          this->cap_pos_.store(pos + 1, std::memory_order_relaxed);
        }
        if (pos + 1 >= this->cap_len_.load(std::memory_order_relaxed))
          this->capturing_.store(false);
      }
    } else {
      // stock-compatible output while unlocked (stock XMOS repeats each sample 3x on both channels)
      if (k == 0) {
        words[2 * out] = l;
        // after a rawtap sync loss, mute ch1 (the R word carries packed data) until relock; fall back to the stock
        // R channel only if no rawtap sync was seen for ~1 s (i.e. the XMOS runs stock firmware)
        const bool mute = this->lock_events_.load(std::memory_order_relaxed) > 0 && this->unlocked_triplets_ < 16000;
        words[2 * out + 1] = mute ? 0 : (int32_t) r;
        this->unlocked_triplets_++;
        out++;
        this->unlocked_samples_++;
        if (this->capturing_.load(std::memory_order_relaxed)) {
          size_t pos = this->cap_pos_.load(std::memory_order_relaxed);
          if (pos < this->cap_len_.load(std::memory_order_relaxed)) {
            int16_t *d = this->cap_buf_ + pos * RAWTAP_CH;
            memset(d, 0, RAWTAP_CH * sizeof(int16_t));
            d[0] = (int16_t) (l >> 16);
            d[1] = (int16_t) (((int32_t) r) >> 16);
            this->cap_pos_.store(pos + 1, std::memory_order_relaxed);
            this->cap_unlocked_++;
          }
          if (pos + 1 >= this->cap_len_.load(std::memory_order_relaxed))
            this->capturing_.store(false);
        }
      }
      for (uint8_t p = 0; p < 3; p++) {
        if (this->cand_run_[p] >= RAWTAP_LOCK_TRIPLETS) {
          this->phase_ = p;
          this->bad_triplets_ = 0;
          if (this->lock_events_.load() > 0 && this->loss_wr_.load() > 0) {
            LossRec &lr = this->loss_ring_[(this->loss_wr_.load() - 1) % LOSS_RING];
            if (lr.phase_delta < 0) {
              lr.phase_delta = (int8_t) ((p + 3 - this->phase_before_loss_) % 3);
              lr.unlocked_triplets = this->unlocked_triplets_;
            }
          }
          this->lock_events_++;
          this->locked_.store(true);
          break;
        }
      }
    }
  }
  return out;
}

std::string Sat1RawtapMicrophone::tap_status() const {
  char buf[96];
  if (this->locked_.load()) {
    snprintf(buf, sizeof(buf), "rawtap locked (locks %u, losses %u, dma ovf %u)", (unsigned) this->lock_events_.load(),
             (unsigned) this->lock_losses_.load(), (unsigned) this->rx_ovf_.load());
  } else if (this->lock_events_.load() == 0) {
    snprintf(buf, sizeof(buf), "stock decode (no rawtap sync)");
  } else {
    snprintf(buf, sizeof(buf), "rawtap UNLOCKED (locks %u, losses %u)", (unsigned) this->lock_events_.load(),
             (unsigned) this->lock_losses_.load());
  }
  return buf;
}

bool Sat1RawtapMicrophone::start_capture(uint32_t ms, bool sweep, float sweep_dbfs) {
  if (this->capturing_.load() || this->dumping_ || this->sweep_playing_) {
    ESP_LOGW(RT_TAG, "capture/dump/sweep already in progress");
    return false;
  }
  if (ms < 100 || ms > 20000) {
    ESP_LOGW(RT_TAG, "capture length %u ms out of range", (unsigned) ms);
    return false;
  }
  size_t n = (size_t) ms * 16;
  if (this->cap_buf_ != nullptr && this->cap_buf_samples_ < n) {
    heap_caps_free(this->cap_buf_);
    this->cap_buf_ = nullptr;
  }
  if (this->cap_buf_ == nullptr) {
    this->cap_buf_ = (int16_t *) heap_caps_malloc(n * RAWTAP_CH * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (this->cap_buf_ == nullptr) {
      ESP_LOGE(RT_TAG, "cannot allocate %u bytes PSRAM for capture", (unsigned) (n * RAWTAP_CH * 2));
      return false;
    }
    this->cap_buf_samples_ = n;
  }
  memset(this->cap_buf_, 0, n * RAWTAP_CH * sizeof(int16_t));
  this->cap_unlocked_.store(0);
  this->cap_pos_.store(0);
  this->cap_len_.store(n);
  this->cap_start_ms_ = millis();
  this->cap_sweep_ = sweep;
  this->sweep_dbfs_ = sweep_dbfs;
#ifdef USE_SPEAKER
  if (sweep && this->sweep_speaker_ == nullptr) {
    ESP_LOGW(RT_TAG, "no sweep_speaker configured; capturing without sweep");
    this->cap_sweep_ = false;
  }
#else
  this->cap_sweep_ = false;
#endif
  if (this->cap_sweep_ && this->sweep_buf_ == nullptr) {
    // 2 s exponential sweep 100 Hz -> 7 kHz at 16 kHz, 20 ms raised-cosine fades
    const float fs = 16000.0f, T = 2.0f, f0 = 100.0f, f1 = 7000.0f;
    this->sweep_len_ = (size_t) (fs * T);
    this->sweep_buf_ = (int16_t *) heap_caps_malloc(this->sweep_len_ * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (this->sweep_buf_ == nullptr) {
      ESP_LOGW(RT_TAG, "cannot allocate sweep buffer; capturing without sweep");
      this->cap_sweep_ = false;
    } else {
      const float amp = powf(10.0f, this->sweep_dbfs_ / 20.0f) * 32767.0f;
      const float L = T / logf(f1 / f0);
      const size_t fade = (size_t) (0.02f * fs);
      for (size_t i = 0; i < this->sweep_len_; i++) {
        float t = i / fs;
        float ph = 2.0f * (float) M_PI * f0 * L * (expf(t / L) - 1.0f);
        float g = 1.0f;
        if (i < fade)
          g = 0.5f - 0.5f * cosf((float) M_PI * i / fade);
        else if (i >= this->sweep_len_ - fade)
          g = 0.5f - 0.5f * cosf((float) M_PI * (this->sweep_len_ - 1 - i) / fade);
        this->sweep_buf_[i] = (int16_t) lrintf(amp * g * sinf(ph));
      }
    }
  }
  this->sweep_pending_ = this->cap_sweep_;
  this->sweep_pos_ = 0;
  this->cap_reported_ = false;
  this->capturing_.store(true);
  ESP_LOGI(RT_TAG, "capture started: %u ms, sweep=%s (%.0f dBFS), status: %s", (unsigned) ms,
           this->cap_sweep_ ? "yes" : "no", this->sweep_dbfs_, this->tap_status().c_str());
  return true;
}

void Sat1RawtapMicrophone::sweep_step_() {
#ifdef USE_SPEAKER
  if (this->sweep_pending_ && millis() - this->cap_start_ms_ >= 1000) {
    this->sweep_pending_ = false;
    this->sweep_speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 1, 16000));
    this->sweep_speaker_->start();
    this->sweep_playing_ = true;
    this->sweep_pos_ = 0;
    ESP_LOGI(RT_TAG, "sweep start at capture t=%u ms", (unsigned) (millis() - this->cap_start_ms_));
  }
  if (this->sweep_playing_) {
    if (this->sweep_pos_ < this->sweep_len_) {
      size_t wrote = this->sweep_speaker_->play((const uint8_t *) (this->sweep_buf_ + this->sweep_pos_),
                                                (this->sweep_len_ - this->sweep_pos_) * sizeof(int16_t), 0);
      this->sweep_pos_ += wrote / sizeof(int16_t);
      if (millis() - this->cap_start_ms_ > 8000 && this->sweep_pos_ < this->sweep_len_) {
        ESP_LOGW(RT_TAG, "sweep timed out after %u samples", (unsigned) this->sweep_pos_);
        this->sweep_pos_ = this->sweep_len_;
      }
    }
    if (this->sweep_pos_ >= this->sweep_len_) {
      this->sweep_speaker_->finish();
      this->sweep_playing_ = false;
      ESP_LOGI(RT_TAG, "sweep queued completely at capture t=%u ms", (unsigned) (millis() - this->cap_start_ms_));
    }
  }
#endif
}

static void rawtap_b64(const uint8_t *src, size_t len, char *dst) {
  static const char *T = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  size_t o = 0;
  for (size_t i = 0; i < len; i += 3) {
    uint32_t v = (uint32_t) src[i] << 16;
    if (i + 1 < len) v |= (uint32_t) src[i + 1] << 8;
    if (i + 2 < len) v |= src[i + 2];
    dst[o++] = T[(v >> 18) & 63];
    dst[o++] = T[(v >> 12) & 63];
    dst[o++] = (i + 1 < len) ? T[(v >> 6) & 63] : '=';
    dst[o++] = (i + 2 < len) ? T[v & 63] : '=';
  }
  dst[o] = 0;
}

bool Sat1RawtapMicrophone::start_dump(uint32_t from_line) {
  if (this->cap_buf_ == nullptr || this->capturing_.load()) {
    ESP_LOGW(RT_TAG, "nothing to dump (no finished capture)");
    return false;
  }
  size_t n = this->cap_pos_.load();
  this->dump_lines_total_ = (n + RAWTAP_SAMPLES_PER_LINE - 1) / RAWTAP_SAMPLES_PER_LINE;
  uint32_t crc = esp_rom_crc32_le(0, (const uint8_t *) this->cap_buf_, n * RAWTAP_CH * sizeof(int16_t));
  ESP_LOGI(RT_TAG, "RTH n=%u ch=7 order=ch0,ch1,ref,E,W,N,S fmt=s16le rate=16000 lines=%u spl=%u crc=%08x unlocked=%u "
           "sweep=%d sweep_dbfs=%.0f status=%s",
           (unsigned) n, (unsigned) this->dump_lines_total_, (unsigned) RAWTAP_SAMPLES_PER_LINE, (unsigned) crc,
           (unsigned) this->cap_unlocked_.load(), this->cap_sweep_ ? 1 : 0, this->sweep_dbfs_,
           this->tap_status().c_str());
  this->dump_line_ = from_line;
  this->dumping_ = true;
  return true;
}

void Sat1RawtapMicrophone::dump_step_() {
  if (!this->dumping_)
    return;
  char b64[RAWTAP_SAMPLES_PER_LINE * RAWTAP_CH * 2 * 4 / 3 + 8];
  const size_t n = this->cap_pos_.load();
  for (int i = 0; i < this->dump_lines_per_loop_ && this->dump_line_ < this->dump_lines_total_; i++) {
    size_t s0 = (size_t) this->dump_line_ * RAWTAP_SAMPLES_PER_LINE;
    size_t cnt = std::min(RAWTAP_SAMPLES_PER_LINE, n - s0);
    rawtap_b64((const uint8_t *) (this->cap_buf_ + s0 * RAWTAP_CH), cnt * RAWTAP_CH * sizeof(int16_t), b64);
    ESP_LOGI(RT_TAG, "RTD %u %s", (unsigned) this->dump_line_, b64);
    this->dump_line_++;
  }
  if (this->dump_line_ >= this->dump_lines_total_) {
    this->dumping_ = false;
    ESP_LOGI(RT_TAG, "RTE lines=%u", (unsigned) this->dump_lines_total_);
  }
}

}  // namespace i2s_audio
}  // namespace esphome

#endif  // USE_ESP32
