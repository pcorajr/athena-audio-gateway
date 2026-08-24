// This file adapts the Hermes OpenAI-compatible API server to the bridge
// contract.
//
// Why the API server and not the webhook adapter: a webhook route spawns the
// agent as a background task and returns "202 accepted" before the run starts
// (gateway/platforms/webhook.py). It is structurally incapable of returning an
// answer. The API server awaits the agent and returns the completed text in the
// response body, which is the request/response shape this bridge needs.
//
// The safety boundary of ADR 0014 lives in parseReply below. A language model
// returns prose; a radio transmits speech. Everything between those two facts
// is this file's responsibility, and it fails closed at every step.

package bridge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// DefaultMaxSpeechChars bounds one radio transmission.
//
// Roughly twenty seconds spoken. A model that returns three paragraphs would
// hold the channel open, so an over-long answer is suppressed rather than
// truncated: half a transmission is worse than none, and a clipped sentence can
// invert its own meaning.
const DefaultMaxSpeechChars = 350

// DefaultModel is the model name the API server advertises for the agent.
const DefaultModel = "hermes-agent"

// fallbackConversation is used when neither a persona nor an explicit
// default names one. Mission is the safer lane to land in: it is read-only.
const fallbackConversation = "athena"

// replyInstruction asks Hermes for a parseable answer.
//
// This is the primary path. It is deliberately paired with a conservative
// fallback in parseReply, because an instruction is a request, not a
// guarantee: a model may ignore it, invent an enum value, or answer in prose.
// Those cases must be safe, not merely unlikely.
const replyInstruction = `You are answering a pilot over a radio. Reply with ONLY a JSON object, no prose and no code fence:
{"speech": "<one or two short spoken sentences>", "intent": "<factual|cue|action_preview|unrecognized>", "state": "<ok|stale|degraded|unavailable|ownship_ambiguous>"}

intent:
- factual: you answered a question about the world.
- cue: you are volunteering information the pilot did not ask for.
- action_preview: the pilot asked you to CHANGE something. Describe what would happen; never act.
- unrecognized: you could not understand the request.

state: ok when your data is current; stale, degraded, unavailable, or ownship_ambiguous when it is qualified.

Keep speech under %d characters and phrase it for speaking aloud, not reading.`

// APIServerConfig configures the adapter.
type APIServerConfig struct {
	// Endpoint is the API server base URL, e.g. http://127.0.0.1:8642.
	Endpoint string
	// APIKey authenticates to the API server. It is a credential: it comes
	// from the environment, never from a command-line flag, and is never
	// logged.
	APIKey string
	// Model is the advertised model name. Empty selects DefaultModel.
	Model string
	// Timeout bounds one exchange. Empty selects DefaultTimeout.
	Timeout time.Duration
	// MaxSpeechChars bounds a deliverable answer. Zero selects
	// DefaultMaxSpeechChars.
	MaxSpeechChars int
	// DefaultConversation is used when a request carries no addressee, which
	// happens in a single-persona deployment. Without it the API server would
	// open a fresh session per transmission, silently destroying continuity in
	// a way that looks like the model forgetting rather than a config error.
	DefaultConversation string
}

// APIServerClient implements Client against the Hermes API server.
type APIServerClient struct {
	endpoint       string
	apiKey         string
	model          string
	maxSpeechChars int
	defaultConv    string
	http           *http.Client
}

// NewAPIServerClient constructs an adapter.
func NewAPIServerClient(cfg APIServerConfig) (*APIServerClient, error) {
	endpoint := strings.TrimRight(strings.TrimSpace(cfg.Endpoint), "/")
	if endpoint == "" {
		return nil, fmt.Errorf("%w: endpoint is required", ErrInvalidRequest)
	}
	if strings.TrimSpace(cfg.APIKey) == "" {
		// Fail at construction rather than on the first transmission. A
		// missing credential discovered mid-flight is a wasted sortie.
		return nil, fmt.Errorf("%w: API key is required", ErrInvalidRequest)
	}

	model := strings.TrimSpace(cfg.Model)
	if model == "" {
		model = DefaultModel
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	maxSpeech := cfg.MaxSpeechChars
	if maxSpeech <= 0 {
		maxSpeech = DefaultMaxSpeechChars
	}
	conv := strings.TrimSpace(cfg.DefaultConversation)
	if conv == "" {
		conv = fallbackConversation
	}

	return &APIServerClient{
		endpoint:       endpoint,
		apiKey:         cfg.APIKey,
		model:          model,
		maxSpeechChars: maxSpeech,
		defaultConv:    conv,
		http:           &http.Client{Timeout: timeout},
	}, nil
}

// responsesRequest is the API server's /v1/responses payload.
type responsesRequest struct {
	Model string `json:"model"`
	Input string `json:"input"`
	// Deliberately no Instructions field.
	//
	// The API server persists `instructions` into a stored conversation, so a
	// reply-shape contract sent that way contaminates every later turn --
	// observed live: a plain question came back wrapped in the radio JSON
	// envelope. The contract is per-turn, so it belongs in Input.
	Conversation string `json:"conversation,omitempty"`
	Store        bool   `json:"store"`
}

// responsesReply is the subset of the API server's response we consume.
type responsesReply struct {
	Output []struct {
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
	} `json:"output"`
}

// Exchange sends a transcript to Hermes and returns a validated response.
//
// Every failure path returns a suppressed response rather than an error where
// the failure is the model's or the transport's, so the caller's contract --
// "an error means nothing may be transmitted" -- is never the only thing
// standing between a bad answer and the radio. Errors are reserved for
// programming faults the caller must fix.
func (c *APIServerClient) Exchange(ctx context.Context, req Request) (Response, error) {
	if err := req.Validate(); err != nil {
		return Response{}, err
	}

	body, err := json.Marshal(responsesRequest{
		Model:        c.model,
		Input:        buildInput(req, c.maxSpeechChars),
		Conversation: c.conversationFor(req),
		Store:        true,
	})
	if err != nil {
		return Response{}, fmt.Errorf("failed to encode API server request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(
		ctx, http.MethodPost, c.endpoint+"/v1/responses", bytes.NewReader(body))
	if err != nil {
		return Response{}, fmt.Errorf("failed to build API server request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+c.apiKey)

	resp, err := c.http.Do(httpReq)
	if err != nil {
		// Timeout, cancellation, or connection failure. Never retried: a late
		// answer on a radio is worse than no answer.
		return Suppressed(req.TransmissionID, IntentUnrecognized, StateUnavailable,
			"hermes unreachable"), nil
	}
	defer func() { _ = resp.Body.Close() }()

	// Bound the read. A runaway body must not exhaust memory on the host that
	// also runs the audio pipeline.
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return Suppressed(req.TransmissionID, IntentUnrecognized, StateDegraded,
			"unreadable hermes reply"), nil
	}

	if resp.StatusCode != http.StatusOK {
		// The status is diagnostic; the body may contain the transcript or key
		// material, so it is not surfaced.
		return Suppressed(req.TransmissionID, IntentUnrecognized, StateUnavailable,
			fmt.Sprintf("hermes returned status %d", resp.StatusCode)), nil
	}

	text, err := extractOutputText(raw)
	if err != nil {
		// Deliberately suppressed rather than returned as an error: a caller
		// that receives an error might one day handle it by transmitting
		// something anyway, whereas a suppressed response cannot carry speech
		// past Response.Validate.
		return Suppressed(req.TransmissionID, IntentUnrecognized, StateDegraded, //nolint:nilerr // fail-closed by design
			"malformed hermes envelope"), nil
	}

	return parseReply(req.TransmissionID, text, c.maxSpeechChars)
}

// conversationFor maps a persona to its own API-server conversation, keeping
// mission and development context separate. Isolation between conversations is
// verified empirically against the live server.
func (c *APIServerClient) conversationFor(req Request) string {
	if addressee := strings.TrimSpace(req.Addressee); addressee != "" {
		return addressee
	}
	return c.defaultConv
}

// buildInput renders the transcript plus the minimum context Hermes needs.
//
// Deliberately small: the request already costs tens of thousands of tokens in
// system prompt and tool schemas, and none of this context earns its place
// unless Hermes would answer differently without it.
func buildInput(req Request, maxSpeechChars int) string {
	var b strings.Builder
	b.WriteString("Radio transmission from ")
	b.WriteString(req.Speaker)
	if addressee := strings.TrimSpace(req.Addressee); addressee != "" {
		b.WriteString(", addressed to ")
		b.WriteString(addressee)
	}
	// strings.Builder never returns an error.
	_, _ = fmt.Fprintf(&b, ", on %.3f MHz %s:\n\n", float64(req.FrequencyHz)/1_000_000, req.Modulation)
	b.WriteString(req.Transcript)

	// The reply-shape contract travels with every turn rather than being set
	// once as session instructions. Repeating it costs a few hundred tokens;
	// leaking it into the conversation costs every future turn in that session.
	b.WriteString("\n\n")
	// strings.Builder never returns an error.
	_, _ = fmt.Fprintf(&b, replyInstruction, maxSpeechChars)
	return b.String()
}

// extractOutputText pulls the assistant text out of the API server envelope,
// concatenating every output_text part across every output item.
func extractOutputText(body []byte) (string, error) {
	var reply responsesReply
	if err := json.Unmarshal(body, &reply); err != nil {
		return "", fmt.Errorf("failed to decode API server envelope: %w", err)
	}
	var b strings.Builder
	for _, item := range reply.Output {
		for _, part := range item.Content {
			if part.Type == "output_text" {
				b.WriteString(part.Text)
			}
		}
	}
	return b.String(), nil
}

// modelReply is the shape requested of Hermes.
//
// Every field is decoded permissively as json.RawMessage-free plain types so
// that a wrong JSON type (a number where a string belongs) surfaces as a
// decode failure and is suppressed, rather than panicking or coercing.
type modelReply struct {
	Speech string `json:"speech"`
	Intent string `json:"intent"`
	State  string `json:"state"`
}

// parseReply converts one model reply into a validated bridge Response.
//
// This is the ADR 0014 safety boundary. It is a pure function so the whole
// decision table can be exercised without a network or a model.
//
// It returns an error only for a programming fault the caller must fix -- an
// empty transmission ID. Every model-originated problem is a suppressed
// response, because the alternative is an error path that a caller might one
// day handle by transmitting something anyway.
func parseReply(transmissionID, raw string, maxSpeechChars int) (Response, error) {
	if strings.TrimSpace(transmissionID) == "" {
		return Response{}, fmt.Errorf("%w: transmission_id is required", ErrInvalidResponse)
	}
	if maxSpeechChars <= 0 {
		maxSpeechChars = DefaultMaxSpeechChars
	}

	candidate, ok := extractJSONObject(raw)
	if !ok {
		return Suppressed(transmissionID, IntentUnrecognized, StateDegraded,
			"reply was not a JSON object"), nil
	}

	// Decode into a local so the error is consumed here rather than returned.
	// Every model-originated problem becomes a suppressed response, never an
	// error: a suppressed response cannot carry speech past Response.Validate,
	// whereas an error leaves the decision to a caller who might one day
	// handle it by transmitting anyway.
	reply, decoded := decodeModelReply(candidate)
	if !decoded {
		return Suppressed(transmissionID, IntentUnrecognized, StateDegraded,
			"reply could not be decoded"), nil
	}

	// Enum values are matched exactly. A near-miss such as "fact" or a
	// different case is rejected rather than coerced: coercion is how
	// "command" silently becomes "factual".
	intent, intentOK := parseIntentClass(reply.Intent)
	if !intentOK {
		return Suppressed(transmissionID, IntentUnrecognized, StateDegraded,
			"reply carried an unknown intent"), nil
	}
	state, stateOK := parseState(reply.State)
	if !stateOK {
		return Suppressed(transmissionID, intent, StateDegraded,
			"reply carried an unknown state"), nil
	}

	// An action-shaped answer is never transmittable, whatever the model
	// claims. Live mutation is out of scope under ADR 0014 and gated by issue
	// #181. The intent is preserved so the log records what was asked for.
	if intent == IntentActionPreview {
		return Suppressed(transmissionID, IntentActionPreview, state,
			"action preview is never transmitted"), nil
	}

	speech := strings.TrimSpace(reply.Speech)
	if speech == "" {
		return Suppressed(transmissionID, intent, state,
			"reply carried no speech"), nil
	}
	if len([]rune(speech)) > maxSpeechChars {
		// Suppressed, not truncated, and the reason does not echo the speech.
		return Suppressed(transmissionID, intent, state,
			"reply was too long to transmit"), nil
	}

	// unrecognized means Hermes could not understand the pilot. Speaking a
	// confused answer is worse than silence.
	if intent == IntentUnrecognized {
		return Suppressed(transmissionID, IntentUnrecognized, state,
			"hermes did not understand the request"), nil
	}

	return Response{
		TransmissionID: transmissionID,
		Deliverable:    true,
		Speech:         speech,
		IntentClass:    intent,
		State:          state,
	}, nil
}

// decodeModelReply decodes the model's JSON object.
//
// It reports success as a boolean rather than an error because there is only
// one correct response to a decode failure here -- suppress -- and returning an
// error would invite a caller to choose differently.
func decodeModelReply(candidate string) (modelReply, bool) {
	var reply modelReply
	if err := json.Unmarshal([]byte(candidate), &reply); err != nil {
		// Includes a field of the wrong JSON type, e.g. "intent": 3.
		return modelReply{}, false
	}
	return reply, true
}

// extractJSONObject finds a JSON object in a reply that may be fenced or
// surrounded by prose.
//
// Recovery is a convenience for models that wrap their answer, not a
// relaxation: whatever is recovered still runs the full decision table above.
func extractJSONObject(raw string) (string, bool) {
	s := strings.TrimSpace(raw)
	if s == "" {
		return "", false
	}

	// Strip a markdown fence if present, with or without a language tag.
	if strings.HasPrefix(s, "```") {
		if idx := strings.Index(s, "\n"); idx >= 0 {
			s = s[idx+1:]
		}
		s = strings.TrimSuffix(strings.TrimSpace(s), "```")
		s = strings.TrimSpace(s)
	}

	start := strings.Index(s, "{")
	end := strings.LastIndex(s, "}")
	if start < 0 || end <= start {
		return "", false
	}
	return s[start : end+1], true
}

// parseIntentClass matches an intent exactly.
func parseIntentClass(s string) (IntentClass, bool) {
	switch IntentClass(s) {
	case IntentFactual:
		return IntentFactual, true
	case IntentCue:
		return IntentCue, true
	case IntentActionPreview:
		return IntentActionPreview, true
	case IntentUnrecognized:
		return IntentUnrecognized, true
	default:
		return "", false
	}
}

// parseState matches a state exactly.
func parseState(s string) (State, bool) {
	switch State(s) {
	case StateOK:
		return StateOK, true
	case StateStale:
		return StateStale, true
	case StateDegraded:
		return StateDegraded, true
	case StateUnavailable:
		return StateUnavailable, true
	case StateOwnshipAmbiguous:
		return StateOwnshipAmbiguous, true
	default:
		return "", false
	}
}

// compile-time assertion that the adapter satisfies the bridge contract.
var _ Client = (*APIServerClient)(nil)

// ensure errors is used for the sentinel comparison in tests of this package.
var _ = errors.Is
