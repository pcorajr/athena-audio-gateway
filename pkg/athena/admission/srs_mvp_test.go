package admission

import (
	"testing"
	"time"
)

func TestSRSMVPExactRouteAndWholeSampleLimit(t *testing.T) {
	cfg := DefaultConfig()
	cfg.FrequencyHz, cfg.Modulation, cfg.PilotName, cfg.FrequencyToleranceHz = 133000000, ModulationAM, "Prometheus", 0
	gate, err := NewGate(cfg)
	if err != nil {
		t.Fatal(err)
	}
	base := Candidate{SpeakerName: "Prometheus", FrequencyHz: 133000000, Modulation: ModulationAM, SampleCount: 480000, Duration: 30 * time.Second}
	if !gate.Admit(base).Admitted {
		t.Fatal("exact boundary rejected")
	}
	for name, mutate := range map[string]func(*Candidate){
		"prefix forbidden":   func(c *Candidate) { c.SampleCount++ },
		"duration overflow":  func(c *Candidate) { c.Duration++ },
		"neighbor frequency": func(c *Candidate) { c.FrequencyHz++ },
		"FM":                 func(c *Candidate) { c.Modulation = ModulationFM },
		"other name":         func(c *Candidate) { c.SpeakerName = "Other" },
		"unresolved":         func(c *Candidate) { c.SpeakerName = "" },
	} {
		t.Run(name, func(t *testing.T) {
			c := base
			mutate(&c)
			if gate.Admit(c).Admitted {
				t.Fatal("candidate admitted")
			}
		})
	}
}
