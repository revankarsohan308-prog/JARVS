#include "audio_output.h"

#include <driver/i2s.h>

#include "config/pins.h"

namespace spk {

// SIMPLE_MASTER+TX on the second I2S controller. The ESP32-S3 has two I2S
// controllers (I2S0/I2S1); I2S0 is owned by the microphone, I2S1 by the
// speaker. If your board exposes fewer controllers this is the one line to
// change.
#define SPK_I2S_PORT I2S_NUM_1

namespace {
bool _active = false;
bool _playing = false;
}  // namespace

bool begin() {
  if (SPK_I2S_BCLK_PIN < 0 || SPK_I2S_LRC_PIN < 0 || SPK_I2S_DIN_PIN < 0) {
    return false;
  }
  i2s_config_t cfg = {
      .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
      .sample_rate = 16000,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,  // mono stream to MAX98357A
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 8,
      .dma_buf_len = 320,
      .use_apll = false,
      .tx_desc_auto_clear = true,
      .fixed_mclk = 0,
  };
  i2s_pin_config_t pins = {
      .bck_io_num = SPK_I2S_BCLK_PIN,
      .ws_io_num = SPK_I2S_LRC_PIN,
      .data_out_num = SPK_I2S_DIN_PIN,
      .data_in_num = I2S_PIN_NO_CHANGE,
  };
  if (i2s_driver_install(SPK_I2S_PORT, &cfg, 0, nullptr) != ESP_OK) {
    return false;
  }
  if (i2s_set_pin(SPK_I2S_PORT, &pins) != ESP_OK) {
    i2s_driver_uninstall(SPK_I2S_PORT);
    return false;
  }
  i2s_zero_dma_buffer(SPK_I2S_PORT);
  _active = true;
  return true;
}

bool active() { return _active; }
bool is_playing() { return _playing; }

void write_pcm(const int16_t* buf, size_t count) {
  if (!_active || !buf || count == 0) return;
  _playing = true;
  size_t written = 0;
  i2s_write(SPK_I2S_PORT, buf, count * sizeof(int16_t), &written, 100);
}

void stop() {
  if (_active) {
    i2s_zero_dma_buffer(SPK_I2S_PORT);
  }
  _playing = false;
}

void play_test_tone() {
  if (!_active) return;
  // Deterministic 1 kHz, 0.3 s beep for SPEAKER TEST.
  static int16_t buf[8000];
  for (size_t i = 0; i < 8000; ++i) {
    buf[i] = (int16_t)(int16_t)(24000 * sin(2.0 * M_PI * 1000.0 * i / 16000.0));
  }
  _playing = true;
  size_t written = 0;
  i2s_write(SPK_I2S_PORT, buf, sizeof(buf), &written, 100);
}

}  // namespace spk