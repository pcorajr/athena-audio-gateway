package bridge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

// ValidateMVP enforces the frozen PTT-only policy. Never truncate speech:
// doing so could remove a material qualification.
func (r Response) ValidateMVP(transmissionID string, maxSpeech int) error {
	if err := r.Validate(); err != nil {
		return err
	}
	if maxSpeech == 0 {
		maxSpeech = DefaultMaxSpeechChars
	}
	if maxSpeech < 1 || maxSpeech > DefaultMaxSpeechChars || r.TransmissionID != transmissionID {
		return fmt.Errorf("%w: correlation or speech limit", ErrInvalidResponse)
	}
	if !utf8.ValidString(r.Speech) || utf8.RuneCountInString(r.Speech) > maxSpeech {
		return fmt.Errorf("%w: speech encoding or length", ErrInvalidResponse)
	}
	if strings.IndexFunc(r.Speech, unicode.IsControl) >= 0 {
		return fmt.Errorf("%w: speech must be plain text", ErrInvalidResponse)
	}
	if r.Deliverable && r.IntentClass != IntentFactual {
		return fmt.Errorf("%w: only factual speech is permitted", ErrInvalidResponse)
	}
	if !r.Deliverable && r.Speech != "" {
		return fmt.Errorf("%w: suppressed speech must be empty", ErrInvalidResponse)
	}
	if !r.validLabels() {
		return fmt.Errorf("%w: route or model label", ErrInvalidResponse)
	}
	return nil
}

// ValidateMVPEndpoint requires an explicit native adapter at a literal loopback
// address. No DNS, credentials, query strings or API-server fallback.
func ValidateMVPEndpoint(endpoint string) error {
	u, err := url.Parse(endpoint)
	if err != nil || u == nil {
		return fmt.Errorf("%w: invalid MVP endpoint", ErrInvalidRequest)
	}
	ip := net.ParseIP(u.Hostname())
	port, portErr := strconv.Atoi(u.Port())
	if u.Scheme != "http" || ip == nil || !ip.IsLoopback() || portErr != nil || port < 1 || port > 65535 ||
		u.User != nil || u.Path != "/radio/v1/exchange" || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.RawPath != "" {
		return fmt.Errorf("%w: MVP requires http://<loopback>:<port>/radio/v1/exchange", ErrInvalidRequest)
	}
	return nil
}

// MVPClient explicitly selects the native bridge, independently of credentials.
// It never retries, follows redirects or uses environment proxy settings.
type MVPClient struct {
	endpoint  string
	http      *http.Client
	maxSpeech int
}

// NewMVPClient constructs the explicit PTT-only native HTTP bridge.
func NewMVPClient(endpoint string, timeout time.Duration, maxSpeech int) (*MVPClient, error) {
	if err := ValidateMVPEndpoint(endpoint); err != nil {
		return nil, err
	}
	if timeout < 0 || maxSpeech < 0 || maxSpeech > DefaultMaxSpeechChars {
		return nil, fmt.Errorf("%w: invalid MVP limits", ErrInvalidRequest)
	}
	if timeout == 0 {
		timeout = DefaultTimeout
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &MVPClient{endpoint: endpoint, maxSpeech: maxSpeech, http: &http.Client{
		Timeout:       timeout,
		Transport:     transport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}, nil
}

func (c *MVPClient) Exchange(ctx context.Context, req Request) (Response, error) {
	if err := req.Validate(); err != nil {
		return Response{}, err
	}
	if req.Addressee != "" || len(req.Transcript) > 32000 || !utf8.ValidString(req.Transcript) {
		return Response{}, fmt.Errorf("%w: invalid MVP transcript or addressee", ErrInvalidRequest)
	}
	body, err := json.Marshal(req)
	if err != nil {
		return Response{}, fmt.Errorf("%w: request encoding", ErrInvalidRequest)
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(body))
	if err != nil {
		return Response{}, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json")
	r, err := c.http.Do(httpReq)
	if err != nil {
		return Response{}, fmt.Errorf("MVP bridge transport failed")
	}
	defer func() { _ = r.Body.Close() }()
	if r.StatusCode != http.StatusOK {
		return Response{}, fmt.Errorf("MVP bridge status %d", r.StatusCode)
	}
	payload, err := io.ReadAll(io.LimitReader(r.Body, maxResponseBytes+1))
	if err != nil || len(payload) > maxResponseBytes {
		return Response{}, fmt.Errorf("%w: response body", ErrInvalidResponse)
	}
	if err := validateMVPJSON(payload); err != nil {
		return Response{}, err
	}
	var resp Response
	if err := json.Unmarshal(payload, &resp); err != nil {
		return Response{}, fmt.Errorf("%w: response encoding", ErrInvalidResponse)
	}
	if err := resp.ValidateMVP(req.TransmissionID, c.maxSpeech); err != nil {
		return Response{}, err
	}
	return resp, nil
}

// encoding/json alone accepts duplicate keys, null booleans and invalid
// Unicode. The frozen response is flat, so validate each field before decoding.
func validateMVPJSON(payload []byte) error {
	invalid := func() error { return fmt.Errorf("%w: malformed MVP JSON", ErrInvalidResponse) }
	if !utf8.Valid(payload) {
		return invalid()
	}
	d := json.NewDecoder(bytes.NewReader(payload))
	first, err := d.Token()
	if err != nil || first != json.Delim('{') {
		return invalid()
	}
	seen := make(map[string]bool)
	for d.More() {
		token, err := d.Token()
		key, ok := token.(string)
		if err != nil || !ok || seen[key] {
			return invalid()
		}
		seen[key] = true
		var value json.RawMessage
		if d.Decode(&value) != nil || bytes.Equal(value, []byte("null")) {
			return invalid()
		}
		switch key {
		case "deliverable":
			var b bool
			if json.Unmarshal(value, &b) != nil {
				return invalid()
			}
		case "transmission_id", "speech", "intent_class", "state", "suppression_reason", "route", "model":
			var s string
			if json.Unmarshal(value, &s) != nil || strings.ContainsRune(s, utf8.RuneError) {
				return invalid()
			}
		default:
			return invalid()
		}
	}
	if _, err := d.Token(); err != nil {
		return invalid()
	}
	for _, key := range []string{"transmission_id", "deliverable", "speech", "intent_class", "state"} {
		if !seen[key] {
			return invalid()
		}
	}
	if _, err := d.Token(); err != io.EOF {
		return invalid()
	}
	return nil
}
