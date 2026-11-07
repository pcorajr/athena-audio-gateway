package bridge

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"unicode/utf8"
)

// StreamClient emits validated complete sentences before the model finishes.
// Each NDJSON frame is a frozen v1 Response, followed by the literal [DONE].
// At most two deliverable frames, 350 runes total; no retries or fallback.
type StreamClient interface {
	ExchangeStream(context.Context, Request, func(Response) error) error
}

func (c *MVPClient) ExchangeStream(ctx context.Context, req Request, emit func(Response) error) error {
	if err := req.Validate(); err != nil {
		return err
	}
	if req.Addressee != "" || len(req.Transcript) > 32000 || !utf8.ValidString(req.Transcript) {
		return ErrInvalidRequest
	}
	body, err := json.Marshal(req)
	if err != nil {
		return ErrInvalidRequest
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(body))
	if err != nil {
		return ErrInvalidRequest
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/x-ndjson")
	reply, err := c.http.Do(request)
	if err != nil {
		return errors.New("stream_transport_failed")
	}
	defer reply.Body.Close()
	if reply.StatusCode != http.StatusOK || strings.Split(reply.Header.Get("Content-Type"), ";")[0] != "application/x-ndjson" {
		return errors.New("stream_protocol_failed")
	}
	scanner := bufio.NewScanner(io.LimitReader(reply.Body, maxResponseBytes+1))
	scanner.Buffer(make([]byte, 1024), maxResponseBytes)
	count, chars, size := 0, 0, 0
	suppressed := false
	previousSpeech := ""
	for scanner.Scan() {
		if ctx.Err() != nil {
			return errors.New("stream_canceled")
		}
		raw := scanner.Bytes()
		size += len(raw) + 1
		if size > maxResponseBytes {
			return ErrInvalidResponse
		}
		if string(raw) == "[DONE]" {
			if count == 0 || scanner.Scan() || scanner.Err() != nil {
				return ErrInvalidResponse
			}
			return nil
		}
		if suppressed || count >= 2 || validateMVPJSON(raw) != nil {
			return ErrInvalidResponse
		}
		var resp Response
		if json.Unmarshal(raw, &resp) != nil || resp.ValidateMVP(req.TransmissionID, c.maxSpeech) != nil {
			return ErrInvalidResponse
		}
		count++
		if resp.Deliverable && resp.Speech == previousSpeech {
			return ErrInvalidResponse
		}
		previousSpeech = resp.Speech
		chars += utf8.RuneCountInString(resp.Speech)
		limit := c.maxSpeech
		if limit == 0 {
			limit = DefaultMaxSpeechChars
		}
		if chars > limit {
			return ErrInvalidResponse
		}
		if !resp.Deliverable {
			if count != 1 {
				return ErrInvalidResponse
			}
			suppressed = true
		}
		if err := emit(resp); err != nil {
			return err
		}
	}
	return errors.New("stream_incomplete")
}
