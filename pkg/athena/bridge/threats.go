package bridge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// ThreatClient is the v15 loopback control plane. It never carries audio or
// chooses a frequency: replyTo is a previously admitted pilot reception.
type ThreatClient interface {
	Remember(context.Context, Request) error
	Poll(context.Context, string, string) (Response, error)
	Receipt(context.Context, string, string) (bool, error)
}

func (c *MVPClient) control(ctx context.Context, path, id string, body any) (Response, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return Response{}, ErrInvalidRequest
	}
	endpoint := strings.TrimSuffix(c.endpoint, "/exchange") + path
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(raw))
	if err != nil {
		return Response{}, ErrInvalidRequest
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Athena-Protocol", "15")
	reply, err := c.http.Do(req)
	if err != nil {
		return Response{}, fmt.Errorf("threat_transport_failed")
	}
	defer reply.Body.Close()
	if reply.StatusCode != http.StatusOK || reply.Header.Get("X-Athena-Protocol") != "15" {
		return Response{}, ErrInvalidResponse
	}
	raw, err = io.ReadAll(io.LimitReader(reply.Body, maxResponseBytes+1))
	if err != nil || len(raw) > maxResponseBytes || validateMVPJSON(raw) != nil {
		return Response{}, ErrInvalidResponse
	}
	var resp Response
	if json.Unmarshal(raw, &resp) != nil || resp.ValidateMVP(id, c.maxSpeech) != nil {
		return Response{}, ErrInvalidResponse
	}
	return resp, nil
}

func (c *MVPClient) Remember(ctx context.Context, req Request) error {
	if req.Validate() != nil || req.Addressee != "" {
		return ErrInvalidRequest
	}
	resp, err := c.control(ctx, "/heard", req.TransmissionID, req)
	if err != nil {
		return err
	}
	if resp.Deliverable || resp.SuppressionReason != "accepted" {
		return ErrInvalidResponse
	}
	return nil
}

func (c *MVPClient) Poll(ctx context.Context, id, replyTo string) (Response, error) {
	resp, err := c.control(ctx, "/unprompted", id, map[string]string{"transmission_id": id, "reply_to": replyTo})
	if err != nil {
		return Response{}, err
	}
	if resp.Deliverable && (resp.Route != "unprompted" || (resp.Priority != "launch" && resp.Priority != "threat") || resp.State != StateOK || len(resp.Speech) > 280) {
		return Response{}, ErrInvalidResponse
	}
	return resp, nil
}

func (c *MVPClient) Receipt(ctx context.Context, id, outcome string) (bool, error) {
	resp, err := c.control(ctx, "/delivery", id, map[string]string{"transmission_id": id, "outcome": outcome})
	if err != nil {
		return false, err
	}
	if resp.Deliverable {
		return false, ErrInvalidResponse
	}
	return resp.SuppressionReason == "accepted", nil
}
