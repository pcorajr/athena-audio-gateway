package bridge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// DefaultTimeout bounds a single Hermes exchange. Radio interaction is
// latency-sensitive: a slow answer is worse than no answer, because the pilot
// has already moved on.
const DefaultTimeout = 10 * time.Second

// maxResponseBytes bounds how much of a reply is read. Hermes returns a short
// radio utterance; anything larger is a malfunction, not a long answer.
const maxResponseBytes = 64 * 1024

// HTTPClient exchanges transcripts with Hermes over HTTP.
//
// It is deliberately unadorned: no retries, no backoff, no queueing. A failed
// exchange means silence on the radio. Retrying would either transmit a stale
// answer to a pilot who has moved on, or double-transmit an answer Hermes
// already produced.
type HTTPClient struct {
	endpoint string
	http     *http.Client
}

// NewHTTPClient constructs a client targeting the given Hermes endpoint.
func NewHTTPClient(endpoint string, timeout time.Duration) (*HTTPClient, error) {
	endpoint = strings.TrimSpace(endpoint)
	if endpoint == "" {
		return nil, fmt.Errorf("%w: endpoint is required", ErrInvalidRequest)
	}
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	return &HTTPClient{
		endpoint: endpoint,
		http:     &http.Client{Timeout: timeout},
	}, nil
}

// Exchange sends a transcript to Hermes and returns its response.
//
// Every error path returns a zero Response alongside the error. Callers must
// treat any error as "transmit nothing"; there is no partial success.
func (c *HTTPClient) Exchange(ctx context.Context, req Request) (Response, error) {
	if err := req.Validate(); err != nil {
		return Response{}, err
	}

	body, err := json.Marshal(req)
	if err != nil {
		return Response{}, fmt.Errorf("failed to encode bridge request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(body))
	if err != nil {
		return Response{}, fmt.Errorf("failed to build bridge request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json")

	httpResp, err := c.http.Do(httpReq)
	if err != nil {
		return Response{}, fmt.Errorf("bridge exchange failed: %w", err)
	}
	defer func() { _ = httpResp.Body.Close() }()

	if httpResp.StatusCode != http.StatusOK {
		return Response{}, fmt.Errorf("bridge returned status %d", httpResp.StatusCode)
	}

	payload, err := io.ReadAll(io.LimitReader(httpResp.Body, maxResponseBytes))
	if err != nil {
		return Response{}, fmt.Errorf("failed to read bridge response: %w", err)
	}

	var resp Response
	if err := json.Unmarshal(payload, &resp); err != nil {
		return Response{}, fmt.Errorf("failed to decode bridge response: %w", err)
	}

	if err := resp.Validate(); err != nil {
		return Response{}, err
	}

	// A reply that answers a different transmission cannot be trusted onto the
	// radio: it would speak one pilot's answer over another's question.
	if resp.TransmissionID != req.TransmissionID {
		return Response{}, fmt.Errorf(
			"%w: response transmission_id %q does not match request %q",
			ErrInvalidResponse, resp.TransmissionID, req.TransmissionID,
		)
	}

	return resp, nil
}
