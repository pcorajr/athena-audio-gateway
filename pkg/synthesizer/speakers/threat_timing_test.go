//go:build !darwin

package speakers

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/synthesizer/voices"
)

// Explicit offline acceptance probe: real embedded Piper, no audio device/SRS.
func TestThreatSpeechTiming(t *testing.T) {
	if os.Getenv("ATHENA_MEASURE_SPEECH") != "1" {
		t.Skip("opt-in real Piper timing")
	}
	lines := []string{
		"Prometheus, Athena, SA-13 in the air, your five o'clock, one mile.",
		"Prometheus, Athena, Caution, SA-13, heat seeker, your one o'clock, eight miles, lethal inside three.",
		"Prometheus, Athena, SA-13 in the air, your seven o'clock, two miles. Also SA-13, your three o'clock, two miles.",
		"SA-13 launch, five o'clock, one mile.",
		"Two SA-13 missiles, seven and three o'clock.",
		"Prometheus, Athena, Two SA-13 missiles, seven and three o'clock.",
		"Caution, SA-13, heat seeker, your one o'clock, eight miles, lethal inside three.",
		"SA-13 missile launch, five o'clock.",
	}
	for _, voice := range []voices.Voice{voices.FeminineVoice, voices.MasculineVoice} {
		for _, speed := range []float64{1, 0.87} {
			speaker, err := NewPiperSpeaker(voice, speed, 200*time.Millisecond)
			if err != nil {
				t.Fatal(err)
			}
			for i, line := range lines {
				audio, err := speaker.SayContext(context.Background(), line)
				if err != nil || len(audio) == 0 {
					t.Fatalf("Piper failed: %v", err)
				}
				seconds := float64(len(audio)) / 16000
				t.Logf("voice=%v speed=%.2f line=%d samples=%d audio_seconds=%.4f", voice, speed, i, len(audio), seconds)

			}
		}
	}
}
