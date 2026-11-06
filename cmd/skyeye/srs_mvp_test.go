package main

import (
	"strings"
	"testing"
	"time"

	"github.com/dharmab/skyeye/internal/cli"
	"github.com/dharmab/skyeye/internal/conf"
	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/spf13/cobra"
	"github.com/spf13/viper"
)

func TestSRSMVPFlagDefaults(t *testing.T) {
	for flag, want := range map[string]string{"athena-srs-mvp": "false", "athena-live-tx": "false", "athena-response-max-age": "10s"} {
		f := skyeye.Flags().Lookup(flag)
		if f == nil || f.DefValue != want {
			t.Fatalf("%s default: %+v", flag, f)
		}
	}
}

func TestSRSMVPConfigBindingFailsClosed(t *testing.T) {
	for _, name := range []string{"athena-srs-mvp", "athena-live-tx", "athena-response-max-age", "mute"} {
		t.Run(name, func(t *testing.T) {
			cmd := &cobra.Command{}
			cmd.Flags().Bool("athena-srs-mvp", false, "")
			cmd.Flags().Bool("athena-live-tx", false, "")
			cmd.Flags().Bool("mute", false, "")
			cmd.Flags().Duration("athena-response-max-age", 10*time.Second, "")
			v := viper.New()
			v.Set(name, "invalid-synthetic-value")
			err := bindFlags(cmd, v)
			if err == nil {
				t.Fatal("invalid configuration silently retained default")
			}
			if strings.Contains(err.Error(), "invalid-synthetic-value") {
				t.Fatal("configuration value leaked into error")
			}
		})
	}
}

func TestSRSMVPConfigBinding(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.Flags().Bool("athena-srs-mvp", false, "")
	cmd.Flags().Bool("athena-live-tx", false, "")
	cmd.Flags().Bool("mute", false, "")
	cmd.Flags().Duration("athena-response-max-age", 10*time.Second, "")
	cmd.Flags().StringSlice("srs-frequencies", nil, "")
	v := viper.New()
	v.SetConfigType("yaml")
	if err := v.ReadConfig(strings.NewReader("athena-srs-mvp: true\nathena-live-tx: false\nmute: true\nathena-response-max-age: 7s\nsrs-frequencies: 133.0AM\n")); err != nil {
		t.Fatal(err)
	}
	if err := bindFlags(cmd, v); err != nil {
		t.Fatal(err)
	}
	if mode, _ := cmd.Flags().GetBool("athena-srs-mvp"); !mode {
		t.Fatal("MVP mode not bound")
	}
	if muted, _ := cmd.Flags().GetBool("mute"); !muted {
		t.Fatal("mute not bound")
	}
	if age, _ := cmd.Flags().GetDuration("athena-response-max-age"); age != 7*time.Second {
		t.Fatal("age not bound")
	}
	frequencies, err := cmd.Flags().GetStringSlice("srs-frequencies")
	if err != nil || validateSRSMVPRadioFlags(frequencies) != nil {
		t.Fatal("one radio not bound")
	}
}

func TestSRSMVPStrictRadioFlags(t *testing.T) {
	for _, frequencies := range [][]string{
		nil, {"133.0AM", "250.0AM"}, {"133.0"}, {"133.0garbage"}, {"133.0garbageAM"}, {"NaNAM"}, {"-1AM"},
		{"133e0AM"}, {"133e0FM"}, {"133.0e0AM"}, {"133.0e0FM"}, {"133E0FM"}, {"133e1AM"},
		{"1_33AM"}, {"0x1.0p7FM"}, {"+133AM"}, {" 133AM"}, {"	133FM"}, {"133..0AM"},
		{""}, {"AM"}, {".FM"}, {"0AM"}, {"InfAM"}, {"-InfFM"}, {"133am"}, {"133Fm"}, {"133 AM FM"},
	} {
		t.Run(strings.Join(frequencies, ","), func(t *testing.T) {
			if validateSRSMVPRadioFlags(frequencies) == nil {
				t.Fatal("invalid radio flags accepted")
			}
		})
	}
	for _, frequency := range []string{"133.0AM", "30.0FM"} {
		if err := validateSRSMVPRadioFlags([]string{frequency}); err != nil {
			t.Fatal(err)
		}
	}
}

// Exercise preRun's validator, run's actual radio/command loaders, and the final
// configuration check in order, without starting models or network clients.
func loadSRSMVPTestConfig(t *testing.T, raw, command string) (conf.Configuration, error) {
	t.Helper()
	frequencies := []string{raw}
	if err := validateSRSMVPRadioFlags(frequencies); err != nil {
		return conf.Configuration{}, err
	}
	radios := cli.LoadFrequencies(frequencies)
	previous := athenaCommandFrequency
	athenaCommandFrequency = command
	t.Cleanup(func() { athenaCommandFrequency = previous })
	hz, modulation := loadAthenaCommandChannel()
	config := conf.Configuration{
		AthenaSRSMVP:             true,
		AthenaResponseMaxAge:     10 * time.Second,
		AthenaCommandFrequencyHz: hz,
		AthenaCommandModulation:  modulation,
		AthenaPilotName:          "Prometheus",
		AthenaHermesEndpoint:     "http://127.0.0.1:1234/radio/v1/exchange",
		SRSFrequencies:           radios,
		Recognizer:               conf.WhisperLocal,
	}
	return config, config.ValidateSRSMVP()
}

func TestSRSMVPExponentRadioStartupRejected(t *testing.T) {
	for _, raw := range []string{"133e0AM", "133e0FM", "133.0e0AM", "133.0e0FM"} {
		t.Run(raw, func(t *testing.T) {
			// An AM command route used to admit even the explicit-FM inputs,
			// because the legacy radio parser silently fell back to AM.
			config, err := loadSRSMVPTestConfig(t, raw, "133.0AM")
			if err == nil {
				t.Fatalf("unsupported radio %q admitted as %+v", raw, config.SRSFrequencies)
			}
		})
	}
}

func TestSRSMVPDecimalRadioStartupPreserved(t *testing.T) {
	for _, tc := range []struct {
		numeric string
		hz      uint64
	}{
		{"133", 133000000}, {"133.0", 133000000}, {"133.025", 133025000},
		{"30.0", 30000000}, {"0133.0", 133000000}, {"133.", 133000000}, {".5", 500000},
	} {
		for _, mod := range []struct {
			suffix string
			value  admission.Modulation
		}{{"AM", admission.ModulationAM}, {"FM", admission.ModulationFM}} {
			for _, spacing := range []bool{false, true} {
				raw := tc.numeric + mod.suffix
				if spacing {
					raw = tc.numeric + " 	" + mod.suffix + " 	"
				}
				t.Run(raw, func(t *testing.T) {
					config, err := loadSRSMVPTestConfig(t, raw, tc.numeric+mod.suffix)
					if err != nil {
						t.Fatal(err)
					}
					if len(config.SRSFrequencies) != 1 || config.SRSFrequencies[0].Frequency.Hertz() != float64(tc.hz) ||
						admission.Modulation(config.SRSFrequencies[0].Modulation) != mod.value {
						t.Fatalf("radio interpretation changed: %+v", config.SRSFrequencies)
					}
					if config.AthenaCommandFrequencyHz != tc.hz || config.AthenaCommandModulation == nil || *config.AthenaCommandModulation != mod.value {
						t.Fatal("command interpretation changed")
					}
				})
			}
		}
	}
}

func TestSRSMVPCommandRadioMismatchRejected(t *testing.T) {
	for _, tc := range []struct{ raw, command string }{
		{"133.0AM", "133.0FM"}, {"133.0FM", "133.0AM"},
		{"133.0AM", "133.025AM"}, {"133.0FM", "133.025FM"},
		{"133.0000005AM", "133.0000005AM"}, {"133.0AM", ""},
	} {
		t.Run(tc.raw+"/"+tc.command, func(t *testing.T) {
			if err := validateSRSMVPRadioFlags([]string{tc.raw}); err != nil {
				t.Fatal("valid decimal rejected before final configuration check:", err)
			}
			if _, err := loadSRSMVPTestConfig(t, tc.raw, tc.command); err == nil {
				t.Fatal("mismatched command route accepted")
			}
		})
	}
}
