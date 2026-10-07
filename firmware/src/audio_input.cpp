#include "audio_input.h"

#include <driver/i2s.h>

#include "config/pins.h"

namespace mic {

namespace {
bool _active = false;
size_t _underruns = 0;
size_t _overruns = 0;
}  // namespace

bool begin() {
  // Honest contract: with any pin unconfigured we do not touch I2S at all.
  if (MIC_I2S_BCLK_PIN < 0 || MIC_I2S_WS_PIN < 0 || MIC_I2S_DATA_PIN < 0) {
    return false;
  }
  i2s_config_t cfg = {
      .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
      .sample_rate = 16000,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,  // INMP441: mono mic on one channel
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 8,
      .dma_buf_len = 320,  // 20 ms of 16 kHz mono
      .use_apll = false,
      .tx_desc_auto_clear = false,
      .fixed_mclk = 0,
  };
  i2s_pin_config_t pins = {
      .bck_io_num = MIC_I2S_BCLK_PIN,
      .ws_io_num = MIC_I2S_WS_PIN,
      .data_out_num = I2S_PIN_NO_CHANGE,
      .data_in_num = MIC_I2S_DATA_PIN,
  };
  if (i2s_driver_install(I2S_NUM_0, &cfg, 0, nullptr) != ESP_OK) {
    return false;
  }
  if (i2s_set_pin(I2S_NUM_0, &pins) != ESP_OK) {
    i2s_driver_uninstall(I2S_NUM_0);
    return false;
  }
  i2s_zero_dma_buffer(I2S_NUM_0);
  _active = true;
  return true;
}

bool active() { return _active; }

bool read(int16_t* out, size_t count) {
  size_t bytes_read = 0;
  esp_err_t err = i2s_read(I2S_NUM_0, out, count * sizeof(int16_t), &bytes_read, 200);
  if (err != ESP_OK || bytes_read < count * sizeof(int16_t)) {
    if (err == ESP_ERR_TIMEOUT) ++_underruns;
    return false;
  }
  return true;
}

float rms_db(const int16_t* buf, size_t count) {
  if (!buf || count == 0) return -120.0f;
  double acc = 0.0;
  for (size_t i = 0; i < count; ++i) {
    const double s = buf[i];
    acc += s * s;
  }
  double rms = sqrt(acc / (double)count);
  if (rms < 1e-9) return -120.0f;
  return (float)(20.0 * log10(rms / 32768.0));
}

size_t read_underruns() { return _underruns; }
size_t read_overruns() { return _overruns; }
void reset_diag() { _underruns = 0; _overruns = 0; }

}  // namespace mic