package main

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"math"
	"math/rand/v2"
	"os"
	"os/signal"
	"reflect"
	"runtime"
	"runtime/pprof"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/cpu"

	"github.com/gofrs/flock"
	"github.com/martinlindhe/unit"
	"github.com/rs/zerolog/log"
	"github.com/spf13/cobra"
	"github.com/spf13/pflag"
	"github.com/spf13/viper"

	"github.com/dharmab/skyeye/internal/application"
	"github.com/dharmab/skyeye/internal/cli"
	"github.com/dharmab/skyeye/internal/conf"
	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/athena/mvp"
	"github.com/dharmab/skyeye/pkg/coalitions"
	"github.com/dharmab/skyeye/pkg/encyclopedia"
	"github.com/dharmab/skyeye/pkg/locations"
	"github.com/dharmab/skyeye/pkg/synthesizer/voices"
	"github.com/ggerganov/whisper.cpp/bindings/go/pkg/whisper"
)

// Used for CLI configuration values.
var (
	configFile                   string
	logLevel                     string
	logFormat                    string
	enableTranscriptionLogging   bool
	acmiFile                     string
	telemetryAddress             string
	telemetryConnectionTimeout   time.Duration
	telemetryPassword            string
	srsAddress                   string
	srsConnectionTimeout         time.Duration
	srsExternalAWACSModePassword string
	srsFrequencies               []string
	enableGRPC                   bool
	grpcAddress                  string
	grpcAPIKey                   string
	controllerCallsign           string
	controllerCallsigns          []string
	coalitionName                string
	telemetryUpdateInterval      time.Duration
	recognizerName               string
	whisperModelPath             string
	whisperThreads               uint
	recognizerLockPath           string
	openAIAPIKey                 string
	voiceName                    string
	useSystemVoice               bool
	mute                         bool
	voiceSpeed                   float64
	voiceVolume                  float64
	voicePauseLength             time.Duration
	voiceLockPath                string
	enableAutomaticPicture       bool
	automaticPictureInterval     time.Duration
	enableThreatMonitoring       bool
	threatMonitoringInterval     time.Duration
	threatMonitoringRequiresSRS  bool
	mandatoryThreatRadiusNM      float64
	threatBRAABearingSpreadDeg   float64
	threatBRAARangeSpreadNM      float64
	enableTracing                bool
	discordWebhookID             string
	discordWebhookToken          string
	exitAfter                    time.Duration
	enableTerrainDetection       bool
	athenaCommandFrequency       string
	athenaSRSMVP                 bool
	athenaLiveTX                 bool
	athenaResponseMaxAge         time.Duration
	athenaPilotName              string
	athenaHermesEndpoint         string
	athenaHermesTimeout          time.Duration
	athenaDebugAudioDir          string
	athenaPromptFile             string
	athenaDynamicPromptFile      string
	athenaPersonas               []string
	athenaPersonaMaxDistance     int
	athenaHermesMaxSpeech        int
	locationsFile                string
	aircraftFile                 string
)

const (
	// voiceVolumeMin is the minimum allowed value for --voice-volume, configuring silence.
	voiceVolumeMin = 0.0
	// voiceVolumeDefault is the default value used for --voice-volume, which configures no change in volume.
	voiceVolumeDefault = 1.0
)

func init() {
	skyeye.Flags().StringVar(&configFile, "config-file", "/etc/skyeye/config.yaml", "Path to config file")

	// Logging
	logLevelFlag := cli.NewEnum(&logLevel, "Level", "info", "error", "warn", "info", "debug", "trace")
	skyeye.Flags().Var(logLevelFlag, "log-level", "Log level (error, warn, info, debug, trace)")
	logFormats := cli.NewEnum(&logFormat, "Format", "pretty", "json")
	skyeye.Flags().Var(logFormats, "log-format", "Log format (pretty, json)")
	skyeye.Flags().BoolVar(&enableTranscriptionLogging, "enable-transcription-logging", true, "Include transcriptions of SRS transmissions in logs and traces")

	// Telemetry
	skyeye.Flags().StringVar(&acmiFile, "acmi-file", "", "path to ACMI file")
	skyeye.Flags().StringVar(&telemetryAddress, "telemetry-address", "localhost:42674", "Address of the real-time telemetry service")
	skyeye.MarkFlagsMutuallyExclusive("acmi-file", "telemetry-address")
	skyeye.Flags().DurationVar(&telemetryConnectionTimeout, "telemetry-connection-timeout", 10*time.Second, "Connection timeout for real-time telemetry client")
	skyeye.Flags().StringVar(&telemetryPassword, "telemetry-password", "", "Password for the real-time telemetry service")
	skyeye.Flags().DurationVar(&telemetryUpdateInterval, "telemetry-update-interval", 2*time.Second, "Interval at which trackfiles are updated from telemetry data")

	// SRS
	skyeye.Flags().StringVar(&srsAddress, "srs-server-address", "localhost:5002", "Address of the SRS server")
	skyeye.Flags().DurationVar(&srsConnectionTimeout, "srs-connection-timeout", 10*time.Second, "Connection timeout for SRS client")
	skyeye.Flags().StringVar(&srsExternalAWACSModePassword, "srs-eam-password", "", "SRS external AWACS mode password")
	skyeye.Flags().StringSliceVar(&srsFrequencies, "srs-frequencies", []string{"251.0AM", "133.0AM", "30.0FM"}, "List of SRS frequencies to use")

	// DCS-gRPC
	skyeye.Flags().BoolVar(&enableGRPC, "enable-grpc", false, "Enable DCS-gRPC features")
	skyeye.Flags().StringVar(&grpcAddress, "grpc-address", "localhost:50051", "Address of the DCS-gRPC server")
	skyeye.Flags().StringVar(&grpcAPIKey, "grpc-api-key", "", "API key for DCS-gRPC authentication")

	// Identity
	skyeye.Flags().StringVar(&controllerCallsign, "callsign", "", "GCI callsign used in radio transmissions. Automatically chosen if not provided")
	skyeye.Flags().StringSliceVar(&controllerCallsigns, "callsigns", []string{}, "A list of GCI callsigns to select from")
	skyeye.MarkFlagsMutuallyExclusive("callsign", "callsigns")
	coalitionFlag := cli.NewEnum(&coalitionName, "Coalition", "blue", "red")
	skyeye.Flags().Var(coalitionFlag, "coalition", "GCI coalition (blue, red)")

	// Speech-to-text
	recognizerFlag := cli.NewEnum(&recognizerName, "Recognizer", string(conf.WhisperLocal), string(conf.WhisperAPI), string(conf.GPT4o), string(conf.GPT4oMini))
	skyeye.Flags().Var(recognizerFlag, "recognizer", "Speech-to-text recognizer to use")
	skyeye.Flags().StringVar(&whisperModelPath, "whisper-model", "", "Path to whisper.cpp model")
	skyeye.Flags().UintVar(&whisperThreads, "whisper-threads", 8, "CPU threads for local whisper.cpp recognition (0 = whisper.cpp default of 4)")
	skyeye.Flags().StringVar(&openAIAPIKey, "openai-api-key", "", "API key for OpenAPI AI")
	skyeye.MarkFlagsOneRequired("whisper-model", "openai-api-key")
	skyeye.Flags().StringVar(&recognizerLockPath, "recognizer-lock-path", "", "Path to lock file for concurrent speech-to-text when using multiple instances")

	// Text-to-speech
	voiceFlag := cli.NewEnum(&voiceName, "Voice", "", "feminine", "masculine")
	skyeye.Flags().Var(voiceFlag, "voice", "Voice to use for SRS transmissions (feminine, masculine). Automatically chosen if not provided.")
	skyeye.Flags().Float64Var(&voiceSpeed, "voice-playback-speed", 1.0, "How quickly the GCI speaks (values below 1.0 are faster and above are slower).")
	skyeye.Flags().Float64Var(&voiceVolume, "voice-volume", voiceVolumeDefault, fmt.Sprintf("Volume level for audio output (%v = silent, %v = normal)", voiceVolumeMin, voiceVolumeDefault))
	skyeye.Flags().BoolVar(&mute, "mute", false, "Mute all SRS transmissions. Useful for testing without disrupting play")

	// Athena command channel (see ADR 0014 in pcorajr/project-athena).
	skyeye.Flags().BoolVar(&athenaSRSMVP, "athena-srs-mvp", false, "PTT-only MVP: one command radio, native bridge, no personas or GCI")
	skyeye.Flags().BoolVar(&athenaLiveTX, "athena-live-tx", false, "MVP voice-write interlock; requires separate live authorization and never overrides --mute")
	skyeye.Flags().DurationVar(&athenaResponseMaxAge, "athena-response-max-age", mvp.DefaultMaxAge, "MVP maximum assembled-reception to first-send age; must be positive")
	// Setting these replaces the GCI controller lane with a text exchange
	// against Hermes. Leaving them unset preserves upstream SkyEye behaviour.
	skyeye.Flags().StringVar(&athenaCommandFrequency, "athena-command-frequency", "", "Athena command channel(s), comma-separated, each with explicit modulation, e.g. 133.0AM,251.0AM,33.0FM. Enables the Athena admission gate; requires --athena-pilot-name. In --athena-srs-mvp mode it defaults to --srs-frequencies and must match it; replies go out on the channel the pilot transmitted on.")
	skyeye.Flags().StringVar(&athenaPilotName, "athena-pilot-name", "", "Exact SRS client name permitted to issue Athena commands. All other transmitters are discarded before speech recognition.")
	skyeye.Flags().StringVar(&athenaHermesEndpoint, "athena-hermes-endpoint", "", "URL of the Hermes text bridge. Replaces the GCI controller lane; requires the Athena admission gate.")
	skyeye.Flags().DurationVar(&athenaHermesTimeout, "athena-hermes-timeout", 0, "Bound on a single Hermes exchange. Zero uses the bridge default. Never retried.")
	skyeye.Flags().StringSliceVar(&athenaPersonas, "athena-persona", nil, "Addressable persona, repeatable, as name[=alias1,alias2]. e.g. --athena-persona=athena --athena-persona=hermes. Defaults to athena and hermes when unset.")
	skyeye.Flags().IntVar(&athenaPersonaMaxDistance, "athena-persona-max-distance", 1, "Edit distance tolerated when matching a spoken persona name, absorbing transcription slips. 0 requires an exact match.")
	skyeye.Flags().IntVar(&athenaHermesMaxSpeech, "athena-hermes-max-speech", 0, "Maximum characters in a transmittable answer. Longer answers are suppressed, not truncated. 0 uses the bridge default.")
	skyeye.Flags().StringVar(&athenaPromptFile, "athena-prompt-file", "", "Path to a file whose contents replace the speech recognizer's initial prompt. Tunes transcription vocabulary without a rebuild.")
	skyeye.Flags().StringVar(&athenaDynamicPromptFile, "athena-dynamic-prompt-file", "", "Path to a file appended to the speech recognizer's prompt on every transmission (callsign, nearby airfields). Re-read when it changes; ignored when missing or stale.")
	skyeye.Flags().StringVar(&athenaDebugAudioDir, "athena-debug-audio-dir", "", "DIAGNOSTIC ONLY: write each admitted transmission's audio to this directory as WAV. Contradicts the ADR 0014 no-retention rule; leave unset for normal operation.")
	skyeye.Flags().StringVar(&voiceLockPath, "voice-lock-path", "", "Path to lock file for concurrent text-to-speech when using multiple instances")
	if runtime.GOOS == "darwin" {
		skyeye.Flags().BoolVar(&useSystemVoice, "use-system-voice", false, "Use the System Voice chosen in the Spoken Content page in System Settings instead of Samantha.")
		if err := skyeye.Flags().MarkDeprecated("voice", "Select a voice in System Settings and use --use-system-voice instead."); err != nil {
			log.Fatal().Err(err).Msg("failed to mark flag as deprecated")
		}
	} else {
		skyeye.Flags().DurationVar(&voicePauseLength, "voice-playback-pause", 200*time.Millisecond, "How long the GCI pauses between sentences.")
	}

	// Controller behavior
	skyeye.Flags().BoolVar(&enableAutomaticPicture, "auto-picture", true, "Enable automatic PICTURE broadcasts")
	skyeye.Flags().DurationVar(&automaticPictureInterval, "auto-picture-interval", 2*time.Minute, "How often to broadcast PICTURE")
	skyeye.Flags().BoolVar(&enableThreatMonitoring, "threat-monitoring", true, "Enable THREAT monitoring")
	skyeye.Flags().DurationVar(&threatMonitoringInterval, "threat-monitoring-interval", 3*time.Minute, "How often to broadcast THREAT")
	skyeye.Flags().Float64Var(&mandatoryThreatRadiusNM, "mandatory-threat-radius", 25, "Briefed radius for mandatory THREAT calls, in nautical miles")
	skyeye.Flags().Float64Var(&threatBRAABearingSpreadDeg, "threat-braa-bearing-spread", 5, "Bearing spread threshold for THREAT call BRAA-vs-bullseye decision, in degrees")
	skyeye.Flags().Float64Var(&threatBRAARangeSpreadNM, "threat-braa-range-spread", 1, "Range spread threshold for THREAT call BRAA-vs-bullseye decision, in nautical miles")
	skyeye.Flags().BoolVar(&threatMonitoringRequiresSRS, "threat-monitoring-requires-srs", true, "Require aircraft to be on SRS to receive THREAT calls. Only useful to disable when debugging")
	skyeye.Flags().StringVar(&locationsFile, "locations-file", "", "Path to file containing additional locations that may be referenced in VECTOR calls.")
	if err := skyeye.MarkFlagFilename("locations-file", "json", "yaml", "yml"); err != nil {
		log.Fatal().Err(err).Msg("failed to mark flag as filename")
	}
	skyeye.Flags().StringVar(&aircraftFile, "aircraft-file", "", "Path to file containing additional aircraft that extend or override the built-in encyclopedia.")
	if err := skyeye.MarkFlagFilename("aircraft-file", "json", "yaml", "yml"); err != nil {
		log.Fatal().Err(err).Msg("failed to mark flag as filename")
	}

	// Tracing
	skyeye.Flags().BoolVar(&enableTracing, "enable-tracing", false, "Enable tracing")
	skyeye.Flags().BoolVar(&enableTracing, "tracing", false, "Enable tracing")
	if err := skyeye.Flags().MarkDeprecated("tracing", "use --enable-tracing instead"); err != nil {
		log.Fatal().Err(err).Msg("failed to mark flag as deprecated")
	}
	skyeye.MarkFlagsMutuallyExclusive("tracing", "enable-tracing")
	skyeye.Flags().StringVar(&discordWebhookID, "discord-webhook-id", "", "Discord webhook ID for tracing")
	skyeye.Flags().StringVar(&discordWebhookToken, "discord-webhook-token", "", "Discord webhook token for tracing")
	skyeye.MarkFlagsRequiredTogether("discord-webhook-id", "discord-webhook-token")

	// Experimental
	skyeye.Flags().BoolVar(&enableTerrainDetection, "x-detect-terrain", false, "Attempt to automatically detect the current terrain to improve coordinate accuracy. This is an experimental feature which may reduce accuracy in some cases")

	// Runtime
	skyeye.Flags().DurationVar(&exitAfter, "exit-after", time.Hour*24*365*20, "Exit after running for the specified duration")
}

// Top-level CLI command.
var skyeye = &cobra.Command{
	Use:     "skyeye",
	Version: Version,
	Short:   "AI Powered GCI Bot for DCS World",
	Long:    "Skyeye uses real-time telemetry data from TacView to provide Ground-Controlled Intercept service over SimpleRadio-Standalone.",
	Example: strings.Join(
		[]string{
			"  " + "Custom Config Path",
			"skyeye --config-file='/home/user/xyz.yaml'",
			"",
			"Remote TacView and SRS server",
			"skyeye --telemetry-address=your-tacview-server:42674 --telemetry-password=your-tacview-password --srs-server-address=your-srs-server:5002 --srs-eam-password=your-srs-eam-password --whisper-model=ggml-small.en.bin",
			"",
			"Local TacView and SRS server",
			"skyeye --telemetry-password=your-tacview-password --srs-eam-password=your-srs-eam-password --whisper-model=ggml-small.en.bin",
		},
		"\n  ",
	),
	PreRunE: preRun,
	Run:     run,
}

// loadAthenaCommandChannels parses the Athena command channels.
//
// Parsing is strict, unlike ParseRadioFrequency: an unrecognized or omitted
// modulation is fatal rather than silently defaulting to AM. On a command
// channel a wrong default is worse than a refusal, because it produces a
// channel that simply never matches and a gateway that appears healthy while
// ignoring every transmission.
func loadAthenaCommandChannels() []admission.Channel {
	channels, err := parseAthenaCommandChannels(athenaCommandFrequency, srsFrequencies, athenaSRSMVP)
	if err != nil {
		log.Fatal().Err(err).Str("input", athenaCommandFrequency).Msg("failed to parse --athena-command-frequency")
	}
	for _, ch := range channels {
		log.Info().
			Uint64("frequencyHz", ch.FrequencyHz).
			Stringer("modulation", ch.Modulation).
			Msg("parsed Athena command channel")
	}
	return channels
}

// parseAthenaCommandChannels parses a comma-separated command channel list.
// In MVP mode an empty list derives from the SRS radio list, so one flag is
// enough; outside MVP an empty list disables the gate (nil, nil).
func parseAthenaCommandChannels(command string, srs []string, mvp bool) ([]admission.Channel, error) {
	entries := strings.Split(command, ",")
	if strings.TrimSpace(command) == "" {
		if !mvp || len(srs) == 0 {
			return nil, nil
		}
		entries = srs
	}
	channels := make([]admission.Channel, 0, len(entries))
	seen := make(map[admission.Channel]bool, len(entries))
	for _, entry := range entries {
		ch, err := parseAthenaChannel(entry)
		if err != nil {
			return nil, err
		}
		if seen[ch] {
			return nil, fmt.Errorf("duplicate Athena command channel %s", ch)
		}
		seen[ch] = true
		channels = append(channels, ch)
	}
	return channels, nil
}

// parseAthenaChannel parses one "<MHz>AM" / "<MHz>FM" entry.
func parseAthenaChannel(entry string) (admission.Channel, error) {
	raw := strings.TrimSpace(entry)
	upper := strings.ToUpper(raw)

	var modulation admission.Modulation
	var numeric string
	switch {
	case strings.HasSuffix(upper, "FM"):
		modulation = admission.ModulationFM
		numeric = strings.TrimSpace(upper[:len(upper)-2])
	case strings.HasSuffix(upper, "AM"):
		modulation = admission.ModulationAM
		numeric = strings.TrimSpace(upper[:len(upper)-2])
	default:
		return admission.Channel{}, errors.New("--athena-command-frequency entries must end in AM or FM, e.g. 30.0FM")
	}

	mhz, err := strconv.ParseFloat(numeric, 64)
	if err != nil {
		return admission.Channel{}, fmt.Errorf("failed to parse --athena-command-frequency entry: %w", err)
	}
	if math.IsNaN(mhz) || math.IsInf(mhz, 0) || mhz <= 0 {
		return admission.Channel{}, errors.New("--athena-command-frequency entries must be real positive numbers")
	}

	return admission.Channel{FrequencyHz: uint64(math.Round(mhz * 1_000_000)), Modulation: modulation}, nil
}

func main() {
	cobra.MousetrapHelpText = "Thanks for trying SkyEye! SkyEye is a command-line application. Please read the documentation on GitHub for instructions on how to run it, or run the program from a terminal to see more help text. "
	cobra.MousetrapDisplayDuration = 0
	if err := skyeye.Execute(); err != nil {
		log.Fatal().Err(err).Msg("application exited with error")
	}
}

func initializeConfig(cmd *cobra.Command) error {
	v := viper.New()

	v.SetConfigFile(configFile)
	if err := v.ReadInConfig(); err != nil {
		// having no config file is fine
		if !errors.Is(err, fs.ErrNotExist) {
			return err
		}
	}

	v.SetEnvPrefix("SKYEYE")
	v.SetEnvKeyReplacer(strings.NewReplacer("-", "_"))
	v.AutomaticEnv()

	return bindFlags(cmd, v)
}

func bindFlags(cmd *cobra.Command, v *viper.Viper) error {
	var configErr error
	cmd.Flags().VisitAll(func(f *pflag.Flag) {
		// Apply the viper config value to the flag when the flag is not set and viper has a value
		if !f.Changed && v.IsSet(f.Name) {
			val := v.Get(f.Name)
			if err := cmd.Flags().Set(f.Name, fmt.Sprint(val)); err != nil {
				// Never silently fall back to defaults for invalid safety flags.
				// Exclude the value/error text, which may contain credentials.
				configErr = errors.Join(configErr, fmt.Errorf("invalid configuration flag %s", f.Name))
			}
		}
	})
	return configErr
}

func loadCoalition() (coalition coalitions.Coalition) {
	log.Info().Str("coalition", coalitionName).Msg("setting GCI coalition")
	switch coalitionName {
	case "blue":
		coalition = coalitions.Blue
	case "red":
		coalition = coalitions.Red
	default:
		log.Fatal().Msg("GCI coalition must be either blue or red")
	}
	log.Info().Int("id", int(coalition)).Msg("GCI coalition set")
	return
}

func loadWhisperModel() *whisper.Model {
	if recognizerName != string(conf.WhisperLocal) {
		return nil
	}
	if whisperModelPath == "" {
		log.Fatal().Msg("whisper-model is required when recognizer is set to " + string(conf.WhisperLocal))
	}
	if runtime.GOARCH == "amd64" && !cpu.X86.HasAVX2 {
		log.Fatal().Msg("The CPU on this machine does not support AVX2 instructions.")
	}

	log.Info().Str("path", whisperModelPath).Msg("loading whisper model")
	whisperModel, err := whisper.New(whisperModelPath)
	if err != nil {
		log.Fatal().Err(err).Str("path", whisperModelPath).Err(err).Msg("failed to load whisper model")
	}
	log.Info().
		Bool("multilingual", whisperModel.IsMultilingual()).
		Strs("languages", whisperModel.Languages()).
		Msg("whisper model loaded")
	return &whisperModel
}

func randomizer() (rando *rand.Rand) {
	hour := time.Now().Hour()
	seed := time.Now().YearDay()
	if 0 <= hour && hour < 8 {
		seed = seed + 1
	} else if 8 <= hour && hour < 16 {
		seed = seed * 2
	} else if 16 <= hour && hour < 24 {
		seed = seed - 3
	}
	rando = rand.New(rand.NewPCG(uint64(seed), uint64(seed))) // nolint:gosec // seed is bounded to small values, so overflow is impossible
	return
}

func loadVoice(rando *rand.Rand) (voice voices.Voice) {
	options := map[string]voices.Voice{
		"feminine":  voices.FeminineVoice,
		"masculine": voices.MasculineVoice,
	}
	if voiceName == "" {
		keys := reflect.ValueOf(options).MapKeys()
		voice = options[keys[rando.IntN(len(keys))].String()]
		log.Info().Type("voice", voice).Msg("randomly selected voice")
	} else {
		voice = options[voiceName]
		log.Info().Type("voice", voice).Msg("selected voice")
	}
	return
}

func loadCallsign(rando *rand.Rand) (callsign string) {
	var options []string
	if controllerCallsign != "" {
		options = append(options, controllerCallsign)
	}
	if len(controllerCallsigns) > 0 {
		options = append(options, controllerCallsigns...)
	}
	if len(options) == 0 {
		options = conf.DefaultCallsigns
	}
	callsign = options[rando.IntN(len(options))]
	if callsign == "" {
		panic("callsign is empty")
	}
	log.Info().Str("callsign", callsign).Msg("selected callsign")
	return
}

func loadLock(path string) *flock.Flock {
	if path == "" {
		return nil
	}
	log.Info().Str("path", path).Msg("using lock file")
	return flock.New(path)
}

func loadVoiceVolume() float64 {
	clamped := max(voiceVolumeMin, min(voiceVolume, voiceVolumeDefault))
	if clamped != voiceVolume {
		log.Warn().Float64("configured", voiceVolume).Float64("clamped", clamped).Msg("clamping voice volume to valid range")
	}
	return clamped
}

func loadLocations() []locations.Location {
	if locationsFile == "" {
		return nil
	}
	data, err := os.ReadFile(locationsFile)
	if err != nil {
		log.Fatal().Err(err).Str("path", locationsFile).Msg("failed to read locations file")
	}
	locs, err := locations.LoadLocations(data)
	if err != nil {
		log.Fatal().Err(err).Str("path", locationsFile).Msg("failed to load locations file")
	}
	log.Info().Int("count", len(locs)).Msg("loaded custom locations")
	return locs
}

func loadAircraft() []encyclopedia.Aircraft {
	if aircraftFile == "" {
		return nil
	}
	data, err := os.ReadFile(aircraftFile)
	if err != nil {
		log.Fatal().Err(err).Str("path", aircraftFile).Msg("failed to read aircraft file")
	}
	aircraft, err := encyclopedia.LoadCustomAircraft(data)
	if err != nil {
		log.Fatal().Err(err).Str("path", aircraftFile).Msg("failed to load aircraft file")
	}
	log.Info().Int("count", len(aircraft)).Msg("loaded custom aircraft")
	return aircraft
}

func preRun(cmd *cobra.Command, _ []string) error {
	if err := initializeConfig(cmd); err != nil {
		return fmt.Errorf("failed to initialize config: %w", err)
	}
	if athenaSRSMVP {
		return validateSRSMVPRadioFlags(srsFrequencies)
	}
	return nil
}

// The legacy parser consumes only a decimal prefix and defaults unknown
// modulation to AM. MVP accepts only spellings that parser preserves.
// Multi-frequency (2026-11-07): 1..conf.MaxSRSMVPRadios distinct entries, each
// with explicit AM or FM, e.g. 133.0AM,251.0AM,33.0FM.
func validateSRSMVPRadioFlags(frequencies []string) error {
	if len(frequencies) == 0 || len(frequencies) > conf.MaxSRSMVPRadios {
		return fmt.Errorf("MVP requires 1..%d srs-frequencies entries", conf.MaxSRSMVPRadios)
	}
	seen := make(map[string]bool, len(frequencies))
	for _, raw := range frequencies {
		key, err := validateSRSMVPRadioFlag(raw)
		if err != nil {
			return err
		}
		if seen[key] {
			return errors.New("MVP srs-frequencies entries must be distinct")
		}
		seen[key] = true
	}
	return nil
}

// validateSRSMVPRadioFlag checks one entry and returns a canonical
// frequency/modulation key for duplicate detection.
func validateSRSMVPRadioFlag(raw string) (string, error) {
	pos := strings.IndexFunc(raw, func(r rune) bool {
		return (r < '0' || r > '9') && r != '.'
	})
	if pos < 0 {
		return "", errors.New("MVP SRS frequency requires decimal MHz and explicit AM or FM")
	}
	suffix := strings.TrimSpace(raw[pos:])
	if suffix != "AM" && suffix != "FM" {
		return "", errors.New("MVP SRS frequency requires explicit AM or FM")
	}
	mhz, err := strconv.ParseFloat(raw[:pos], 64)
	if err != nil || math.IsNaN(mhz) || math.IsInf(mhz, 0) || mhz <= 0 {
		return "", errors.New("MVP SRS frequency must be a real positive number")
	}
	return strconv.FormatFloat(mhz, 'f', -1, 64) + suffix, nil
}

func run(_ *cobra.Command, _ []string) {
	// Set up an application-scoped context and a cancel function to shut down the application.
	ctx, cancel := context.WithCancel(context.Background())

	// Safety in case of hung routine
	go func() {
		<-ctx.Done()
		time.Sleep(10 * time.Second)
		log.Warn().Msg("shutdown took too long, forcing exit")
		_ = pprof.Lookup("goroutine").WriteTo(os.Stderr, 1)
		os.Exit(1)
	}()

	var wg sync.WaitGroup

	cli.SetupZerolog(logLevel, logFormat)

	log.Info().Str("version", Version).Msg("SkyEye GCI Bot")

	log.Info().Msg("setting up interrupt and TERM signal handler")
	interuptChan := make(chan os.Signal, 1)
	signal.Notify(interuptChan, os.Interrupt, syscall.SIGTERM)
	go func() {
		s := <-interuptChan
		log.Info().Any("signal", s).Msg("received shutdown signal")
		cancel()
		wg.Wait()
		os.Exit(0)
	}()

	log.Info().Msg("loading configuration")
	coalition := loadCoalition()
	rando := randomizer()
	voice := loadVoice(rando)
	callsign := loadCallsign(rando)
	parsedSRSFrequencies := cli.LoadFrequencies(srsFrequencies)
	voiceLock := loadLock(voiceLockPath)
	recognizerLock := loadLock(recognizerLockPath)
	volume := loadVoiceVolume()
	locs := loadLocations()
	customAircraft := loadAircraft()
	athenaChannels := loadAthenaCommandChannels()
	var athenaFrequencyHz uint64
	var athenaModulation *admission.Modulation
	if len(athenaChannels) > 0 {
		athenaFrequencyHz = athenaChannels[0].FrequencyHz
		primary := athenaChannels[0].Modulation
		athenaModulation = &primary
	}

	config := conf.Configuration{
		ACMIFile:                     acmiFile,
		TelemetryAddress:             telemetryAddress,
		TelemetryConnectionTimeout:   telemetryConnectionTimeout,
		TelemetryClientName:          callsign,
		TelemetryPassword:            telemetryPassword,
		SRSAddress:                   srsAddress,
		SRSConnectionTimeout:         srsConnectionTimeout,
		SRSClientName:                fmt.Sprintf("GCI %s [BOT]", callsign),
		SRSExternalAWACSModePassword: srsExternalAWACSModePassword,
		SRSFrequencies:               parsedSRSFrequencies,
		EnableTranscriptionLogging:   enableTranscriptionLogging,
		Callsign:                     callsign,
		Coalition:                    coalition,
		RadarSweepInterval:           telemetryUpdateInterval,
		Recognizer:                   conf.Recognizer(recognizerName),
		RecognizerLock:               recognizerLock,
		OpenAIAPIKey:                 openAIAPIKey,
		Voice:                        voice,
		UseSystemVoice:               useSystemVoice,
		VoiceLock:                    voiceLock,
		Mute:                         mute,
		AthenaCommandFrequencyHz:     athenaFrequencyHz,
		AthenaSRSMVP:                 athenaSRSMVP,
		AthenaLiveTX:                 athenaLiveTX,
		AthenaResponseMaxAge:         athenaResponseMaxAge,
		WhisperThreads:               whisperThreads,
		AthenaCommandModulation:      athenaModulation,
		AthenaCommandChannels:        athenaChannels,
		AthenaPilotName:              athenaPilotName,
		AthenaHermesEndpoint:         athenaHermesEndpoint,
		AthenaHermesTimeout:          athenaHermesTimeout,
		AthenaHermesMaxSpeech:        athenaHermesMaxSpeech,
		AthenaPersonas:               athenaPersonas,
		AthenaPersonaMaxDistance:     athenaPersonaMaxDistance,
		AthenaPromptFile:             athenaPromptFile,
		AthenaDynamicPromptFile:      athenaDynamicPromptFile,
		AthenaDebugAudioDir:          athenaDebugAudioDir,
		VoiceSpeed:                   voiceSpeed,
		Volume:                       volume,
		VoicePauseLength:             voicePauseLength,
		EnableAutomaticPicture:       enableAutomaticPicture,
		PictureBroadcastInterval:     automaticPictureInterval,
		EnableThreatMonitoring:       enableThreatMonitoring,
		ThreatMonitoringInterval:     threatMonitoringInterval,
		ThreatMonitoringRequiresSRS:  threatMonitoringRequiresSRS,
		MandatoryThreatRadius:        unit.Length(mandatoryThreatRadiusNM) * unit.NauticalMile,
		ThreatBRAABearingSpread:      unit.Angle(threatBRAABearingSpreadDeg) * unit.Degree,
		ThreatBRAARangeSpread:        unit.Length(threatBRAARangeSpreadNM) * unit.NauticalMile,
		EnableTracing:                enableTracing,
		DiscordWebhookID:             discordWebhookID,
		DiscorbWebhookToken:          discordWebhookToken,
		ExitAfter:                    exitAfter,
		EnableGRPC:                   enableGRPC,
		GRPCAddress:                  grpcAddress,
		GRPCAPIKey:                   grpcAPIKey,
		EnableTerrainDetection:       enableTerrainDetection,
		Locations:                    locs,
		CustomAircraft:               customAircraft,
	}

	if err := config.ValidateSRSMVP(); err != nil {
		log.Fatal().Err(err).Msg("invalid MVP configuration")
	}
	config.WhisperModel = loadWhisperModel()
	log.Info().Msg("starting application")
	app, err := application.NewApplication(config)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to start application")
	}
	err = app.Run(ctx, cancel, &wg)
	if err != nil {
		log.Fatal().Err(err).Msg("application exited with error")
	}
	wg.Wait()
}
