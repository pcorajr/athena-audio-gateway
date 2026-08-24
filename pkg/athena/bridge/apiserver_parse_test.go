package bridge

import (
	"errors"
	"strings"
	"testing"
)

// These tests are the specification for the ADR 0014 safety boundary at the
// point where a language model's prose becomes something a radio can transmit.
//
// parseReply is a pure function precisely so this decision table can be
// exercised exhaustively with no network and no model. Every rule below has
// exactly one job: make it impossible for an unsafe answer to carry speech.

const testTransmissionID = "tx-parse-1"

// assertSuppressed is the invariant every fail-closed path must satisfy: no
// speech survives, and the suppression is explained.
func assertSuppressed(t *testing.T, got Response, wantReasonContains string) {
	t.Helper()

	if got.Deliverable {
		t.Errorf("response is deliverable; want suppressed")
	}
	if strings.TrimSpace(got.Speech) != "" {
		t.Errorf("suppressed response carries speech %q; no speech may survive", got.Speech)
	}
	if strings.TrimSpace(got.SuppressionReason) == "" {
		t.Error("suppressed response must explain itself")
	}
	if wantReasonContains != "" && !strings.Contains(got.SuppressionReason, wantReasonContains) {
		t.Errorf("suppression reason = %q, want it to mention %q",
			got.SuppressionReason, wantReasonContains)
	}
	// A suppressed response must still be a valid response; the silent path is
	// held to the same standard as the speaking one.
	if err := got.Validate(); err != nil {
		t.Errorf("suppressed response fails Validate: %v", err)
	}
}

func TestParseReplyAcceptsAWellFormedFactualAnswer(t *testing.T) {
	t.Parallel()

	got, err := parseReply(testTransmissionID,
		`{"speech":"You are flying an A-10C two.","intent":"factual","state":"ok"}`, 350)
	if err != nil {
		t.Fatalf("parseReply: %v", err)
	}
	if !got.Deliverable {
		t.Error("a well-formed factual answer should be deliverable")
	}
	if got.Speech != "You are flying an A-10C two." {
		t.Errorf("speech = %q, want it passed through verbatim", got.Speech)
	}
	if got.IntentClass != IntentFactual {
		t.Errorf("intent = %q, want %q", got.IntentClass, IntentFactual)
	}
	if got.State != StateOK {
		t.Errorf("state = %q, want %q", got.State, StateOK)
	}
	if got.TransmissionID != testTransmissionID {
		t.Errorf("transmission_id = %q, want %q", got.TransmissionID, testTransmissionID)
	}
	if err := got.Validate(); err != nil {
		t.Errorf("deliverable response fails Validate: %v", err)
	}
}

// The single most important rule in this file.
//
// Live mutation is out of scope under ADR 0014 and gated by issue #181. An
// action-shaped answer must never reach the radio, no matter how confidently
// the model asserts it is deliverable.
func TestActionPreviewIsNeverDeliverable(t *testing.T) {
	t.Parallel()

	for _, raw := range []string{
		`{"speech":"Spawning a tank at sector alpha.","intent":"action_preview","state":"ok"}`,
		`{"speech":"Tank spawned.","intent":"action_preview","state":"ok","deliverable":true}`,
		`{"speech":"Confirm spawn?","intent":"action_preview","state":"degraded"}`,
	} {
		got, err := parseReply(testTransmissionID, raw, 350)
		if err != nil {
			t.Fatalf("parseReply(%q): %v", raw, err)
		}
		assertSuppressed(t, got, "action")
		if got.IntentClass != IntentActionPreview {
			t.Errorf("intent = %q, want %q preserved for the record",
				got.IntentClass, IntentActionPreview)
		}
	}
}

// A model that ignores the reply-shape instruction must not have its prose
// synthesized onto the radio.
func TestUnparseableReplyIsSuppressed(t *testing.T) {
	t.Parallel()

	for name, raw := range map[string]string{
		"plain prose":      "You are flying an A-10C. Nearest threat is a SAM.",
		"empty":            "",
		"whitespace":       "   \n  ",
		"truncated json":   `{"speech":"You are flying an A-1`,
		"json array":       `["speech","factual"]`,
		"json string":      `"just a string"`,
		"json null":        `null`,
		"markdown fenced":  "here you go:\n```\nnot json\n```",
		"number":           `42`,
		"unrelated object": `{"foo":"bar"}`,
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			got, err := parseReply(testTransmissionID, raw, 350)
			if err != nil {
				t.Fatalf("parseReply must not error, it must suppress: %v", err)
			}
			assertSuppressed(t, got, "")
			if got.IntentClass != IntentUnrecognized {
				t.Errorf("intent = %q, want %q for an unusable reply",
					got.IntentClass, IntentUnrecognized)
			}
		})
	}
}

// An invented enum value must be rejected, never coerced to the nearest known
// value. Coercion is how "command" silently becomes "factual".
func TestUnknownEnumValueIsRejectedNotCoerced(t *testing.T) {
	t.Parallel()

	for name, raw := range map[string]string{
		"unknown intent":    `{"speech":"ok","intent":"command","state":"ok"}`,
		"unknown state":     `{"speech":"ok","intent":"factual","state":"excellent"}`,
		"empty intent":      `{"speech":"ok","intent":"","state":"ok"}`,
		"empty state":       `{"speech":"ok","intent":"factual","state":""}`,
		"missing intent":    `{"speech":"ok","state":"ok"}`,
		"missing state":     `{"speech":"ok","intent":"factual"}`,
		"intent wrong case": `{"speech":"ok","intent":"FACTUAL","state":"ok"}`,
		"numeric intent":    `{"speech":"ok","intent":3,"state":"ok"}`,
		"intent near-miss":  `{"speech":"ok","intent":"fact","state":"ok"}`,
		"state near-miss":   `{"speech":"ok","intent":"factual","state":"okay"}`,
		"nonsense both":     `{"speech":"ok","intent":"x","state":"y"}`,
		"null intent":       `{"speech":"ok","intent":null,"state":"ok"}`,
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			got, err := parseReply(testTransmissionID, raw, 350)
			if err != nil {
				t.Fatalf("parseReply must not error, it must suppress: %v", err)
			}
			assertSuppressed(t, got, "")
		})
	}
}

// Contradictory: claims deliverable but has nothing to say. Trust the safer
// reading.
func TestEmptySpeechIsSuppressed(t *testing.T) {
	t.Parallel()

	for name, raw := range map[string]string{
		"empty speech":      `{"speech":"","intent":"factual","state":"ok"}`,
		"whitespace speech": `{"speech":"   ","intent":"factual","state":"ok"}`,
		"missing speech":    `{"intent":"factual","state":"ok"}`,
		"null speech":       `{"speech":null,"intent":"factual","state":"ok"}`,
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			got, err := parseReply(testTransmissionID, raw, 350)
			if err != nil {
				t.Fatalf("parseReply must not error, it must suppress: %v", err)
			}
			assertSuppressed(t, got, "")
		})
	}
}

// A three-paragraph answer would hold the radio open. Suppress rather than
// truncate: half a transmission is worse than none, and a clipped sentence can
// invert its own meaning.
func TestOverlongSpeechIsSuppressedNotTruncated(t *testing.T) {
	t.Parallel()

	long := strings.Repeat("a", 400)
	got, err := parseReply(testTransmissionID,
		`{"speech":"`+long+`","intent":"factual","state":"ok"}`, 350)
	if err != nil {
		t.Fatalf("parseReply: %v", err)
	}
	assertSuppressed(t, got, "long")
	if strings.Contains(got.SuppressionReason, long[:50]) {
		t.Error("suppression reason must not echo the speech back")
	}
}

func TestSpeechAtExactlyTheLimitIsAccepted(t *testing.T) {
	t.Parallel()

	exact := strings.Repeat("a", 350)
	got, err := parseReply(testTransmissionID,
		`{"speech":"`+exact+`","intent":"factual","state":"ok"}`, 350)
	if err != nil {
		t.Fatalf("parseReply: %v", err)
	}
	if !got.Deliverable {
		t.Error("speech at exactly the limit should be accepted; the boundary must not be off by one")
	}
}

// The model must not be able to influence correlation. transmission_id is set
// by the adapter from the request, always.
func TestTransmissionIDIsAlwaysSetByTheAdapter(t *testing.T) {
	t.Parallel()

	for name, raw := range map[string]string{
		"model supplies its own": `{"speech":"ok","intent":"factual","state":"ok","transmission_id":"attacker"}`,
		"well formed":            `{"speech":"ok","intent":"factual","state":"ok"}`,
		"unparseable":            `not json at all`,
		"action preview":         `{"speech":"ok","intent":"action_preview","state":"ok"}`,
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			got, err := parseReply(testTransmissionID, raw, 350)
			if err != nil {
				t.Fatalf("parseReply: %v", err)
			}
			if got.TransmissionID != testTransmissionID {
				t.Errorf("transmission_id = %q, want %q; the model must not influence correlation",
					got.TransmissionID, testTransmissionID)
			}
		})
	}
}

// A cue is a deliverable non-answer: Athena volunteering something. It is
// allowed to speak.
func TestCueIsDeliverable(t *testing.T) {
	t.Parallel()

	got, err := parseReply(testTransmissionID,
		`{"speech":"Threat bearing zero niner zero.","intent":"cue","state":"ok"}`, 350)
	if err != nil {
		t.Fatalf("parseReply: %v", err)
	}
	if !got.Deliverable {
		t.Error("a cue should be deliverable")
	}
	if got.IntentClass != IntentCue {
		t.Errorf("intent = %q, want %q", got.IntentClass, IntentCue)
	}
}

// Every degraded state is still speakable -- the qualification is the point,
// and the pilot needs to hear "stale" rather than silence.
func TestDegradedStatesRemainDeliverable(t *testing.T) {
	t.Parallel()

	for _, state := range []State{StateOK, StateStale, StateDegraded, StateUnavailable, StateOwnshipAmbiguous} {
		raw := `{"speech":"Data may be stale.","intent":"factual","state":"` + string(state) + `"}`
		got, err := parseReply(testTransmissionID, raw, 350)
		if err != nil {
			t.Fatalf("parseReply(%q): %v", state, err)
		}
		if !got.Deliverable {
			t.Errorf("state %q should still be deliverable with a qualification", state)
		}
		if got.State != state {
			t.Errorf("state = %q, want %q preserved", got.State, state)
		}
	}
}

// Models commonly wrap JSON in a markdown fence or a sentence. Recovering the
// object is a convenience, not a relaxation: everything recovered still runs
// the full decision table.
func TestJSONEmbeddedInProseIsRecovered(t *testing.T) {
	t.Parallel()

	for name, raw := range map[string]string{
		"fenced":         "```json\n{\"speech\":\"ok\",\"intent\":\"factual\",\"state\":\"ok\"}\n```",
		"fenced bare":    "```\n{\"speech\":\"ok\",\"intent\":\"factual\",\"state\":\"ok\"}\n```",
		"leading prose":  "Here is the answer: {\"speech\":\"ok\",\"intent\":\"factual\",\"state\":\"ok\"}",
		"trailing prose": "{\"speech\":\"ok\",\"intent\":\"factual\",\"state\":\"ok\"} -- hope that helps",
		"whitespace":     "\n\n  {\"speech\":\"ok\",\"intent\":\"factual\",\"state\":\"ok\"}  \n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			got, err := parseReply(testTransmissionID, raw, 350)
			if err != nil {
				t.Fatalf("parseReply: %v", err)
			}
			if !got.Deliverable {
				t.Errorf("embedded JSON should be recovered and delivered, got suppressed: %q",
					got.SuppressionReason)
			}
		})
	}
}

// Recovery must not become a licence to guess: an action_preview wrapped in
// prose is still an action_preview.
func TestRecoveredJSONStillObeysTheDecisionTable(t *testing.T) {
	t.Parallel()

	got, err := parseReply(testTransmissionID,
		"Sure! ```json\n{\"speech\":\"Spawning.\",\"intent\":\"action_preview\",\"state\":\"ok\"}\n```", 350)
	if err != nil {
		t.Fatalf("parseReply: %v", err)
	}
	assertSuppressed(t, got, "action")
}

func TestParseReplyRejectsAnEmptyTransmissionID(t *testing.T) {
	t.Parallel()

	// A response with no correlation cannot be matched to a transmission and
	// would fail Validate downstream. Catch it here, loudly.
	if _, err := parseReply("", `{"speech":"ok","intent":"factual","state":"ok"}`, 350); err == nil {
		t.Fatal("an empty transmission id must be an error")
	} else if !errors.Is(err, ErrInvalidResponse) {
		t.Errorf("error = %v, want it to wrap ErrInvalidResponse", err)
	}
}
