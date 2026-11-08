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
		nil, {"133.0"}, {"133.0garbage"}, {"133.0garbageAM"}, {"NaNAM"}, {"-1AM"},
		{"133e0AM"}, {"133e0FM"}, {"133.0e0AM"}, {"133.0e0FM"}, {"133E0FM"}, {"133e1AM"},
		{"1_33AM"}, {"0x1.0p7FM"}, {"+133AM"}, {" 133AM"}, {"	133FM"}, {"133..0AM"},
		{""}, {"AM"}, {".FM"}, {"0AM"}, {"InfAM"}, {"-InfFM"}, {"133am"}, {"133Fm"}, {"133 AM FM"},
		// Multi-frequency: every entry must be valid, explicit and distinct.
		{"133.0AM", "251.0"}, {"133.0AM", "251.0AM", "33.0garbage"}, {"133.0AM", ""},
		{"133.0AM", "133AM"}, {"133.0AM", "133.000AM"}, {"251.0AM", "33.0FM", "251AM"},
		{"1AM", "2AM", "3AM", "4AM", "5AM", "6AM", "7AM", "8AM", "9AM", "10AM", "11AM"},
	} {
		t.Run(strings.Join(frequencies, ","), func(t *testing.T) {
			if validateSRSMVPRadioFlags(frequencies) == nil {
				t.Fatal("invalid radio flags accepted")
			}
		})
	}
	for _, frequencies := range [][]string{
		{"133.0AM"}, {"30.0FM"}, {"133.0AM", "251.0AM", "33.0FM"}, {"133.0AM", "133.0FM"},
		{"1AM", "2AM", "3AM", "4AM", "5AM", "6AM", "7AM", "8AM", "9AM", "10AM"},
	} {
		if err := validateSRSMVPRadioFlags(frequencies); err != nil {
			t.Fatal(err)
		}
	}
}

// Exercise preRun's validator, run's actual radio/command loaders, and the final
// configuration check in order, without starting models or network clients.
func loadSRSMVPTestConfig(t *testing.T, raw, command string) (conf.Configuration, error) {
	t.Helper()
	return loadSRSMVPTestConfigList(t, []string{raw}, command)
}

func loadSRSMVPTestConfigList(t *testing.T, frequencies []string, command string) (conf.Configuration, error) {
	t.Helper()
	if err := validateSRSMVPRadioFlags(frequencies); err != nil {
		return conf.Configuration{}, err
	}
	radios := cli.LoadFrequencies(frequencies)
	channels, err := parseAthenaCommandChannels(command, frequencies, true)
	if err != nil {
		return conf.Configuration{}, err
	}
	var hz uint64
	var modulation *admission.Modulation
	if len(channels) > 0 {
		hz = channels[0].FrequencyHz
		m := channels[0].Modulation
		modulation = &m
	}
	config := conf.Configuration{
		AthenaSRSMVP:             true,
		AthenaResponseMaxAge:     10 * time.Second,
		AthenaCommandFrequencyHz: hz,
		AthenaCommandModulation:  modulation,
		AthenaCommandChannels:    channels,
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
		{"133.0000005AM", "133.0000005AM"}, {"133.0AM", "133.0"}, {"133.0AM", "133.0AM,251.0AM"},
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

// Multi-frequency (2026-11-07): 133.0 AM (A-10C VHF AM), 251.0 AM (UHF, F-4E
// and A-10C), 33.0 FM (A-10C VHF FM).
func TestSRSMVPMultiFrequencyStartup(t *testing.T) {
	want := []admission.Channel{
		{FrequencyHz: 133000000, Modulation: admission.ModulationAM},
		{FrequencyHz: 251000000, Modulation: admission.ModulationAM},
		{FrequencyHz: 33000000, Modulation: admission.ModulationFM},
	}
	srs := []string{"133.0AM", "251.0AM", "33.0FM"}
	for _, command := range []string{"133.0AM,251.0AM,33.0FM", "", "33.0FM, 133.0AM ,251.0am"} {
		t.Run(command, func(t *testing.T) {
			config, err := loadSRSMVPTestConfigList(t, srs, command)
			if err != nil {
				t.Fatal(err)
			}
			if len(config.SRSFrequencies) != 3 || len(config.AthenaCommandChannels) != 3 {
				t.Fatalf("radios=%+v channels=%+v", config.SRSFrequencies, config.AthenaCommandChannels)
			}
			got := map[admission.Channel]bool{}
			for _, ch := range config.AthenaCommandChannels {
				got[ch] = true
			}
			for _, ch := range want {
				if !got[ch] {
					t.Fatalf("missing channel %s in %+v", ch, config.AthenaCommandChannels)
				}
			}
			if command == "" && (config.AthenaCommandChannels[0] != want[0] || config.AthenaCommandFrequencyHz != 133000000) {
				t.Fatal("derived channels must follow srs-frequencies order")
			}
		})
	}
	for _, tc := range []struct {
		srs     []string
		command string
	}{
		{srs, "133.0AM,251.0AM"},                // command subset of radios
		{srs, "133.0AM,251.0AM,33.0AM"},         // wrong modulation on one entry
		{srs, "133.0AM,251.0AM,33.0FM,40.0FM"},  // unlisted extra channel
		{srs, "133.0AM,251.0AM,33.0FM,133.0AM"}, // duplicate
		{srs, "133.0AM,,33.0FM"},                // empty entry
		{[]string{"133.0AM", "251.0AM"}, "133.0AM,251.0AM,33.0FM"},
	} {
		t.Run(strings.Join(tc.srs, ",")+"/"+tc.command, func(t *testing.T) {
			if _, err := loadSRSMVPTestConfigList(t, tc.srs, tc.command); err == nil {
				t.Fatal("mismatched multi-frequency configuration accepted")
			}
		})
	}
}

func TestSRSMVPMultiFrequencyFlagBinding(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.Flags().StringSlice("srs-frequencies", nil, "")
	cmd.Flags().String("athena-command-frequency", "", "")
	if err := cmd.Flags().Parse([]string{"--srs-frequencies=133.0AM,251.0AM,33.0FM", "--athena-command-frequency=133.0AM,251.0AM,33.0FM"}); err != nil {
		t.Fatal(err)
	}
	frequencies, _ := cmd.Flags().GetStringSlice("srs-frequencies")
	command, _ := cmd.Flags().GetString("athena-command-frequency")
	if len(frequencies) != 3 {
		t.Fatalf("srs-frequencies split into %d entries", len(frequencies))
	}
	if _, err := loadSRSMVPTestConfigList(t, frequencies, command); err != nil {
		t.Fatal(err)
	}
}
