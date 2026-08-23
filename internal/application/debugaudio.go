package application

import (
	"encoding/binary"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/dharmab/skyeye/pkg/pcm/rate"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	"github.com/rs/zerolog/log"
)

// wavHeaderSize is the size of a canonical 44-byte RIFF/WAVE header.
const wavHeaderSize = 44

// bitsPerSample is the sample width used for diagnostic captures. The gateway
// works in F32 internally; 16-bit PCM is used on disk because every audio tool
// reads it without argument.
const bitsPerSample = 16

// writeDiagnosticWAV writes one transmission's decoded audio to a 16-bit mono
// WAV file for offline inspection.
//
// SAFETY: ADR 0014 states that audio never leaves the gateway process. This
// function deliberately breaks that rule and therefore exists only as an
// operator-invoked diagnostic:
//
//   - it is unreachable unless --athena-debug-audio-dir names a directory;
//   - enabling it logs a prominent warning at startup;
//   - it is never set in the shipped systemd unit.
//
// Its purpose is to answer a question that cannot be answered from inside the
// process: what does the audio actually sound like once SRS has finished with
// it. Once that is settled the flag should be left unset.
func writeDiagnosticWAV(dir, transmissionID string, samples []float32) error {
	if dir == "" {
		return nil
	}
	if len(samples) == 0 {
		return nil
	}

	name := transmissionID
	if name == "" {
		name = time.Now().UTC().Format("20060102T150405.000")
	}
	// Guard against a trace ID containing path separators.
	path := filepath.Join(dir, filepath.Base(name)+".wav")

	// Bound the capture so the 32-bit WAV size fields cannot overflow. A WAV
	// header stores sizes as uint32; at 16 kHz mono 16-bit that is ~37 hours,
	// far beyond any transmission, but the cap makes the conversions below
	// provably safe rather than merely improbable.
	const maxSamples = 1 << 26 // ~70 minutes at 16 kHz
	if len(samples) > maxSamples {
		log.Warn().Int("samples", len(samples)).Msg("truncating oversized diagnostic capture")
		samples = samples[:maxSamples]
	}

	sampleRate := uint32(rate.Wideband.Hertz())
	dataSize := uint32(len(samples) * (bitsPerSample / 8)) //nolint:gosec // bounded by maxSamples above

	buf := make([]byte, 0, wavHeaderSize+int(dataSize))
	buf = append(buf, []byte("RIFF")...)
	buf = binary.LittleEndian.AppendUint32(buf, 36+dataSize)
	buf = append(buf, []byte("WAVEfmt ")...)
	buf = binary.LittleEndian.AppendUint32(buf, 16) // PCM fmt chunk size
	buf = binary.LittleEndian.AppendUint16(buf, 1)  // PCM
	buf = binary.LittleEndian.AppendUint16(buf, 1)  // mono
	buf = binary.LittleEndian.AppendUint32(buf, sampleRate)
	buf = binary.LittleEndian.AppendUint32(buf, sampleRate*(bitsPerSample/8)) // byte rate
	buf = binary.LittleEndian.AppendUint16(buf, bitsPerSample/8)              // block align
	buf = binary.LittleEndian.AppendUint16(buf, bitsPerSample)
	buf = append(buf, []byte("data")...)
	buf = binary.LittleEndian.AppendUint32(buf, dataSize)

	for _, s := range samples {
		// Clamp before conversion so a hot signal saturates rather than wraps
		// around into loud noise, which would misrepresent the recording.
		if s > 1 {
			s = 1
		} else if s < -1 {
			s = -1
		}
		buf = binary.LittleEndian.AppendUint16(buf, uint16(int16(s*32767))) //nolint:gosec // deliberate two's-complement reinterpretation for PCM16
	}

	if err := os.WriteFile(path, buf, 0o600); err != nil {
		return fmt.Errorf("failed to write diagnostic WAV: %w", err)
	}

	log.Warn().
		Str("path", path).
		Int("samples", len(samples)).
		Float64("seconds", float64(len(samples))/rate.Wideband.Hertz()).
		Msg("DIAGNOSTIC: wrote transmission audio to disk")
	return nil
}

// captureDiagnosticAudio writes a transmission's audio when diagnostic capture
// is enabled. Failures are logged and swallowed: a diagnostic must never break
// the pipeline it is diagnosing.
func (a *Application) captureDiagnosticAudio(transmission simpleradio.Transmission) {
	if a.debugAudioDir == "" {
		return
	}
	if err := writeDiagnosticWAV(a.debugAudioDir, transmission.TraceID, transmission.Audio); err != nil {
		log.Error().Err(err).Msg("failed to capture diagnostic audio")
	}
}
