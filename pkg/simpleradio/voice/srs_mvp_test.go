package voice

import (
	"encoding/binary"
	"testing"
)

func TestSRSMVPSingleRadioFraming(t *testing.T) {
	packet := testPacket([]byte{1, 2, 3}, []Frequency{{Frequency: 133000000, Modulation: 0}})
	encoded := packet.Encode()
	decoded, err := Decode(encoded)
	if err != nil || len(decoded.Frequencies) != 1 || decoded.Frequencies[0].Frequency != 133000000 || decoded.Frequencies[0].Modulation != 0 {
		t.Fatalf("route did not round trip: %v", err)
	}
	for name, mutate := range map[string]func([]byte) []byte{
		"truncated":             func(b []byte) []byte { return b[:len(b)-1] },
		"trailing":              func(b []byte) []byte { return append(b, 0) },
		"bad frequency segment": func(b []byte) []byte { binary.LittleEndian.PutUint16(b[4:6], 9); return b },
		"bad audio segment":     func(b []byte) []byte { binary.LittleEndian.PutUint16(b[2:4], 65535); return b },
	} {
		t.Run(name, func(t *testing.T) {
			b := append([]byte(nil), encoded...)
			if _, err := Decode(mutate(b)); err == nil {
				t.Fatal("malformed packet accepted")
			}
		})
	}
}
