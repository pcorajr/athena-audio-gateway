package main

import (
	"strings"
	"testing"
	"time"

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
	for _, frequencies := range [][]string{nil, {"133.0AM", "250.0AM"}, {"133.0"}, {"133.0garbage"}, {"133.0garbageAM"}, {"NaNAM"}, {"-1AM"}} {
		if validateSRSMVPRadioFlags(frequencies) == nil {
			t.Fatal("invalid radio flags accepted")
		}
	}
	for _, frequency := range []string{"133.0AM", "30.0FM"} {
		if err := validateSRSMVPRadioFlags([]string{frequency}); err != nil {
			t.Fatal(err)
		}
	}
}
