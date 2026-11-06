package simpleradio

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"time"

	"github.com/dharmab/skyeye/pkg/simpleradio/types"
	"github.com/rs/zerolog/log"
)

// connectTCP connects to the SRS server over TCP.
func (c *Client) connectTCP() error {
	log.Info().Str("address", c.address).Msg("connecting to SRS server TCP socket")
	tcpAddress, err := net.ResolveTCPAddr("tcp", c.address)
	if err != nil {
		return fmt.Errorf("failed to resolve SRS server address %v: %w", c.address, err)
	}
	connection, err := net.DialTCP("tcp", nil, tcpAddress)
	if err != nil {
		return fmt.Errorf("failed to connect to data socket: %w", err)
	}
	c.connectionLock.Lock()
	c.tcpConnection = connection
	c.connectionLock.Unlock()
	return nil
}

// connectUDP connects to the SRS server over UDP.
func (c *Client) connectUDP() error {
	log.Info().Str("address", c.address).Msg("connecting to SRS server UDP socket")
	udpAddress, err := net.ResolveUDPAddr("udp", c.address)
	if err != nil {
		return fmt.Errorf("failed to resolve SRS server address %v: %w", c.address, err)
	}
	connection, err := net.DialUDP("udp", nil, udpAddress)
	if err != nil {
		return fmt.Errorf("failed to connect to UDP socket: %w", err)
	}
	c.connectionLock.Lock()
	defer c.connectionLock.Unlock()
	c.udpConnection = connection
	c.clientsLock.Lock()
	if c.mvp {
		c.mvpEpoch++
		c.clients = make(map[types.GUID]types.ClientInfo)
	}
	c.mvpWriter = connection
	c.clientsLock.Unlock()
	return nil
}

// reconnect closes the existing connections and attempts to reconnect to the
// SRS server. It will retry until successful or the context is canceled.
func (c *Client) reconnect(ctx context.Context) error {
	c.connectionLock.Lock()
	c.invalidateMVP()
	c.connectionLock.Unlock()
	var err error
	backoff := frameLength

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
			log.Info().Msg("attempting to reconnect to SRS server")
			_ = c.tcp().Close()
			err = c.connectTCP()
			if err == nil {
				log.Info().Msg("successfully reconnected to SRS server over TCP")
				_ = c.udp().Close()
				err = c.connectUDP()
				if err == nil {
					log.Info().Msg("successfully reconnected to SRS server over UDP")
					return nil
				}
			}
			time.Sleep(backoff)
			backoff = min(time.Duration(float64(backoff)*math.Sqrt2), time.Minute)

			log.Error().Err(err).Stringer("retryIn", backoff).Msg("failed to reconnect to SRS server, retrying")
		}
	}
}

// receiveUDP listens for incoming UDP packets and routes them to the appropriate channel.
func (c *Client) receiveUDP(ctx context.Context, pingChan chan<- []byte, voiceChan chan<- voiceDatagram) {
	for {
		select {
		case <-ctx.Done():
			log.Info().Msg("stopping SRS packet receiver due to context cancellation")
			return
		default:
			buf := make([]byte, 1500)
			c.connectionLock.RLock()
			c.clientsLock.RLock()
			epoch := c.mvpEpoch
			connection := c.udpConnection
			c.clientsLock.RUnlock()
			c.connectionLock.RUnlock()
			n, err := connection.Read(buf)
			switch {
			case errors.Is(err, net.ErrClosed):
				if ctx.Err() == nil {
					log.Error().Err(err).Msg("UDP connection closed")
					time.Sleep(5 * time.Millisecond)
				}
			case errors.Is(err, io.EOF):
				log.Error().Err(err).Msg("UDP connection returned EOF")
			case err != nil:
				log.Error().Err(err).Msg("UDP connection read error")
			case n == 0:
				log.Warn().Err(err).Msg("0 bytes read from UDP connection")
			default:
				packet := make([]byte, n)
				copy(packet, buf)
				switch {
				case n < types.GUIDLength:
					log.Debug().Int("bytes", n).Msg("UDP packet smaller than expected")
				case n == types.GUIDLength:
					// Ping packet
					pingChan <- packet
				case n > types.GUIDLength:
					// Voice packet
					// The epoch is captured before Read: a datagram buffered on
					// an old connection cannot be admitted after reconnect.
					select {
					case voiceChan <- voiceDatagram{data: packet, epoch: epoch}:
					case <-ctx.Done():
						return
					}
				}
			}
		}
	}
}

// receiveTCP listens for incoming TCP messages and routes them to the appropriate handler.
func (c *Client) receiveTCP(ctx context.Context) {
	connection := c.tcp()
	reader := bufio.NewReader(connection)
	for {
		select {
		case <-ctx.Done():
			log.Info().Msg("stopping SRS client due to context cancellation")
			return
		default:
			line, err := reader.ReadBytes(byte('\n'))
			if err != nil {
				c.connectionLock.RLock()
				if connection == c.tcpConnection {
					c.invalidateMVP()
				}
				c.connectionLock.RUnlock()
				if errors.Is(err, net.ErrClosed) && ctx.Err() != nil {
					continue
				}
				log.Error().Err(err).Msg("error reading from SRS server TCP socket")
				// Wait and try again in case it recovers by reconnecting
				time.Sleep(pingInterval)
				connection = c.tcp()
				reader = bufio.NewReader(connection)
				continue
			}

			var message types.Message
			jsonErr := json.Unmarshal(line, &message)
			if jsonErr != nil {
				log.Warn().Str("text", string(line)).Err(jsonErr).Msg("failed to unmarshal message")
			} else {
				c.handleConnectionMessage(connection, message)
			}
		}
	}
}

// Serialize identity updates against reconnect. The password acknowledgement
// only emits a radio announcement; release the pointer lock before its Send.
func (c *Client) handleConnectionMessage(connection *net.TCPConn, message types.Message) {
	c.connectionLock.RLock()
	if connection != c.tcpConnection {
		c.connectionLock.RUnlock()
		return
	}
	if message.Type == types.MessageExternalAWACSModePassword {
		c.connectionLock.RUnlock()
		c.handleMessage(message)
		return
	}
	defer c.connectionLock.RUnlock()
	c.handleMessage(message)
}

func (c *Client) tcp() *net.TCPConn {
	c.connectionLock.RLock()
	defer c.connectionLock.RUnlock()
	return c.tcpConnection
}

func (c *Client) udp() *net.UDPConn {
	c.connectionLock.RLock()
	defer c.connectionLock.RUnlock()
	return c.udpConnection
}
