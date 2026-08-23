package application

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"

	"github.com/dharmab/skyeye/pkg/simpleradio"
)

// transmissionWithAudio builds a transmission carrying n samples of audio.
func transmissionWithAudio(id string, n int) simpleradio.Transmission {
	return simpleradio.Transmission{
		TraceID: id,
		Audio:   make(simpleradio.Audio, n),
	}
}

// The capture is a deliberate exception to ADR 0014. The most important
// property is therefore not that it writes correctly, but that it writes
// nothing at all unless an operator explicitly asked for it.
func TestDiagnosticCaptureIsOffByDefault(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()

	// No directory configured: nothing may be written, even given real audio.
	if err := writeDiagnosticWAV("", "tx-1", []float32{0.1, 0.2}); err != nil {
		t.Fatalf("disabled capture should be a no-op, got %v", err)
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	if len(entries) != 0 {
		t.Errorf("disabled capture wrote %d files; want 0", len(entries))
	}

	// An Application with no debugAudioDir must not write either.
	app := &Application{}
	app.captureDiagnosticAudio(transmissionWithAudio("tx-2", 16000))
	entries, err = os.ReadDir(dir)
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	if len(entries) != 0 {
		t.Errorf("Application with no debugAudioDir wrote %d files; want 0", len(entries))
	}
}

func TestDiagnosticCaptureWritesPlayableWAV(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	samples := make([]float32, 16000) // one second at wideband
	for i := range samples {
		samples[i] = 0.5
	}

	if err := writeDiagnosticWAV(dir, "tx-1", samples); err != nil {
		t.Fatalf("writeDiagnosticWAV: %v", err)
	}

	data, err := os.ReadFile(filepath.Join(dir, "tx-1.wav"))
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}

	if got := string(data[0:4]); got != "RIFF" {
		t.Errorf("magic = %q, want RIFF", got)
	}
	if got := string(data[8:12]); got != "WAVE" {
		t.Errorf("format = %q, want WAVE", got)
	}
	if got := binary.LittleEndian.Uint16(data[22:24]); got != 1 {
		t.Errorf("channels = %d, want 1 (mono)", got)
	}
	if got := binary.LittleEndian.Uint32(data[24:28]); got != 16000 {
		t.Errorf("sample rate = %d, want 16000", got)
	}
	if got := binary.LittleEndian.Uint16(data[34:36]); got != 16 {
		t.Errorf("bits per sample = %d, want 16", got)
	}

	wantBytes := wavHeaderSize + len(samples)*2
	if len(data) != wantBytes {
		t.Errorf("file size = %d, want %d", len(data), wantBytes)
	}
}

// A hot signal must saturate rather than wrap, or the recording would
// misrepresent the audio it exists to diagnose.
func TestDiagnosticCaptureClampsRatherThanWraps(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	if err := writeDiagnosticWAV(dir, "tx-clip", []float32{2.0, -2.0}); err != nil {
		t.Fatalf("writeDiagnosticWAV: %v", err)
	}

	data, err := os.ReadFile(filepath.Join(dir, "tx-clip.wav"))
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}

	first := int16(binary.LittleEndian.Uint16(data[wavHeaderSize : wavHeaderSize+2]))    //nolint:gosec // reading back PCM16
	second := int16(binary.LittleEndian.Uint16(data[wavHeaderSize+2 : wavHeaderSize+4])) //nolint:gosec // reading back PCM16

	if first <= 0 {
		t.Errorf("+2.0 clamped to %d; a positive sample must stay positive", first)
	}
	if second >= 0 {
		t.Errorf("-2.0 clamped to %d; a negative sample must stay negative", second)
	}
}

// A trace ID is server-supplied. It must not be able to steer the write out of
// the configured directory.
func TestDiagnosticCaptureConfinesPathTraversal(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	if err := writeDiagnosticWAV(dir, "../../escaped", []float32{0.1}); err != nil {
		t.Fatalf("writeDiagnosticWAV: %v", err)
	}

	if _, err := os.Stat(filepath.Join(dir, "escaped.wav")); err != nil {
		t.Errorf("expected the file inside the configured directory: %v", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	if len(entries) != 1 {
		t.Errorf("wrote %d files; want exactly 1 inside the directory", len(entries))
	}
}

func TestDiagnosticCaptureIgnoresEmptyAudio(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	if err := writeDiagnosticWAV(dir, "tx-empty", nil); err != nil {
		t.Fatalf("writeDiagnosticWAV: %v", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	if len(entries) != 0 {
		t.Errorf("empty audio wrote %d files; want 0", len(entries))
	}
}
