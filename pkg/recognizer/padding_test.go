package recognizer

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestPadLeadInAddsFourHundredMillisecondsOfSilence(t *testing.T) {
	t.Parallel()
	in := []float32{0.5, -0.25, 1}
	out := padLeadIn(in)
	const want = 6400 // 0.4 s at 16 kHz
	require.Len(t, out, want+len(in))
	for i := range want {
		if out[i] != 0 {
			t.Fatalf("lead-in sample %d = %v, want silence", i, out[i])
		}
	}
	assert.Equal(t, in, out[want:])
	assert.Equal(t, []float32{0.5, -0.25, 1}, in, "input must not be modified")
}

func TestPadLeadInEmptySample(t *testing.T) {
	t.Parallel()
	assert.Len(t, padLeadIn(nil), 6400)
}
