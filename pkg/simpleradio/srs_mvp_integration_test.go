package simpleradio_test

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/admission"
	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/mvp"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

// startTextFixture consumes the controller-frozen SRS-H launcher interface.
// It is not a replacement adapter: the accepted adapter supplies its actual
// HTTP validation/rendering handler and deterministic fake state/model peers.
func startTextFixture(t *testing.T, scenario string) string {
	t.Helper()
	root, src, python := os.Getenv("SRSMVP_TEXT_ADAPTER_ROOT"), os.Getenv("SRSMVP_ATHENA_SRC"), os.Getenv("SRSMVP_PYTHON")
	for name, path := range map[string]string{"adapter root": root, "Athena src": src, "Python": python} {
		if !filepath.IsAbs(path) {
			t.Fatalf("%s must be an explicitly approved absolute path", name)
		}
	}
	cmd := exec.Command(python, "-B", "-m", "integrations.hermes_radio.fixture_server", "--port", "0", "--scenario", scenario)
	cmd.Dir = root
	// Do not inherit credentials, model routes or production configuration.
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "HOME=" + t.TempDir(), "TMPDIR=" + t.TempDir(), "PYTHONPATH=" + src + string(os.PathListSeparator) + root, "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1"}
	cmd.Stderr = io.Discard
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatalf("fixture start failed: %v", err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	t.Cleanup(func() {
		if err := cmd.Process.Signal(syscall.SIGTERM); err != nil && err != os.ErrProcessDone {
			t.Errorf("fixture shutdown signal: %v", err)
		}
		select {
		case err := <-done:
			if err != nil {
				t.Errorf("fixture did not exit cleanly: %v", err)
			}
		case <-time.After(5 * time.Second):
			_ = cmd.Process.Kill()
			<-done
			t.Error("fixture required forced teardown")
		}
	})
	type readyResult struct {
		line string
		err  error
	}
	ready := make(chan readyResult, 1)
	go func() {
		scanner := bufio.NewScanner(io.LimitReader(stdout, 4096))
		if !scanner.Scan() {
			ready <- readyResult{err: fmt.Errorf("no readiness line")}
			return
		}
		ready <- readyResult{line: scanner.Text()}
		_, _ = io.Copy(io.Discard, stdout)
	}()
	select {
	case result := <-ready:
		if result.err != nil {
			t.Fatal(result.err)
		}
		var message struct {
			Ready bool   `json:"ready"`
			URL   string `json:"url"`
		}
		decoder := json.NewDecoder(strings.NewReader(result.line))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&message); err != nil || !message.Ready || !strings.HasPrefix(message.URL, "http://127.0.0.1:") || bridge.ValidateMVPEndpoint(message.URL) != nil {
			t.Fatal("invalid fixture readiness envelope")
		}
		if decoder.Decode(new(any)) != io.EOF {
			t.Fatal("trailing fixture readiness data")
		}
		return message.URL
	case <-time.After(10 * time.Second):
		t.Fatal("fixture readiness timed out")
	}
	return ""
}

type bridgeFunc func(context.Context, bridge.Request) (bridge.Response, error)

func (f bridgeFunc) Exchange(ctx context.Context, req bridge.Request) (bridge.Response, error) {
	return f(ctx, req)
}

func TestSRSMVPTextRoundTrip(t *testing.T) {
	if os.Getenv("SRSMVP_TEXT_ADAPTER_ROOT") == "" {
		t.Skip("controller composition gate: accepted SRS-H fixture root not supplied")
	}
	for _, scenario := range []string{"current_state", "recent_events", "unavailable", "stale", "ambiguous", "unrecognized"} {
		t.Run(scenario, func(t *testing.T) {
			url := startTextFixture(t, scenario)
			client, err := bridge.NewMVPClient(url, 5*time.Second, 350)
			if err != nil {
				t.Fatal(err)
			}
			for _, mode := range []string{"normal", "wrong_correlation", "late_reply", "muted", "live_tx_off", "duplicate"} {
				t.Run(mode, func(t *testing.T) {
					radio, tr, writes := simpleradio.NewSRSMVPFixtureClient(mode == "muted", mode != "live_tx_off")
					tr.TraceID = "synthetic-" + scenario + "-" + mode
					cfg := admission.DefaultConfig()
					cfg.FrequencyHz = 133000000
					cfg.Modulation = admission.ModulationAM
					cfg.PilotName = "Prometheus"
					cfg.FrequencyToleranceHz = 0
					gate, err := admission.NewGate(cfg)
					if err != nil {
						t.Fatal(err)
					}
					text := "What is my current status?"
					if scenario == "recent_events" {
						text = "What are the latest recorded events?"
					}
					if scenario == "unrecognized" {
						text = "Synthetic unrelated question about cooking."
					}
					var observed bridge.Response
					var timings []mvp.Timing
					exchanges := 0
					syntheses := 0
					r := &mvp.Runner{Gate: gate, Radio: radio, MaxAge: mvp.DefaultMaxAge,
						Recognize: func(context.Context, simpleradio.Audio) (string, error) { return text, nil },
						Synthesize: func(_ context.Context, speech string) (simpleradio.Audio, error) {
							syntheses++
							if speech != observed.Speech {
								t.Error("adapter speech was rewritten")
							}
							return make(simpleradio.Audio, 1280), nil
						},
						Record: func(timing mvp.Timing) { timings = append(timings, timing) },
					}
					if mode == "late_reply" {
						r.MaxAge = 2 * time.Second
					}
					r.Bridge = bridgeFunc(func(ctx context.Context, req bridge.Request) (bridge.Response, error) {
						exchanges++
						response, err := client.Exchange(ctx, req)
						if err != nil {
							t.Errorf("actual adapter exchange failed: %v", err)
							return response, err
						}
						observed = response
						if mode == "wrong_correlation" {
							response.TransmissionID = "wrong-synthetic-id"
						}
						if mode == "late_reply" {
							<-ctx.Done()
						}
						return response, nil
					})
					r.Process(t.Context(), tr)
					if mode == "duplicate" {
						r.Process(t.Context(), tr)
					}
					if exchanges != 1 || len(timings) != 1 {
						t.Fatalf("exchange/replay count=%d timings=%d", exchanges, len(timings))
					}
					// Unrecognized requests are suppressed without querying state.
					wantState := map[string]bridge.State{"current_state": bridge.StateOK, "recent_events": bridge.StateOK, "unavailable": bridge.StateUnavailable, "stale": bridge.StateStale, "ambiguous": bridge.StateOwnshipAmbiguous, "unrecognized": bridge.StateUnavailable}[scenario]
					if observed.State != wantState {
						t.Fatalf("scenario %s state=%s", scenario, observed.State)
					}
					wantDeliverable := scenario != "unrecognized"
					if observed.Deliverable != wantDeliverable {
						t.Fatalf("scenario deliverable=%v", observed.Deliverable)
					}
					if scenario == "unrecognized" {
						if observed.IntentClass != bridge.IntentUnrecognized || observed.SuppressionReason != "unrecognized_intent" || observed.Speech != "" {
							t.Fatalf("unexpected unrecognized suppression: intent=%q reason=%q speech=%q", observed.IntentClass, observed.SuppressionReason, observed.Speech)
						}
						if observed.TransmissionID != tr.TraceID || timings[0].TransmissionID != tr.TraceID {
							t.Fatalf("unrecognized correlation: response=%q timing=%q want=%q", observed.TransmissionID, timings[0].TransmissionID, tr.TraceID)
						}
						if syntheses != 0 || timings[0].TTSDone != -1 {
							t.Fatalf("unrecognized reply reached synthesis: calls=%d tts_done=%d", syntheses, timings[0].TTSDone)
						}
					}
					wantWrites := 0
					if wantDeliverable && (mode == "normal" || mode == "duplicate") {
						wantWrites = 2
					}
					if writes() != wantWrites {
						t.Fatalf("unexpected real transport/fake writer calls: %d, want %d", writes(), wantWrites)
					}
					if wantWrites == 0 && (timings[0].FirstSend != -1 || timings[0].LastSend != -1) {
						t.Fatal("suppressed reply reported packet writes")
					}
				})
			}
		})
	}
}
