// Package mqtt is a minimal MQTT 3.1.1 publisher built on the standard library,
// so the gateway compiles to one static binary with no module downloads. That
// matters on an air-gapped build host.
//
// It publishes at QoS 0 only, which matches how S2 treats periodic telemetry
// (ADR 0008: a lost reading is acceptable, the next one follows within a
// second). Anything that must not be lost needs QoS 1 and a full client such as
// eclipse/paho.mqtt.golang behind the same Publish method.
package mqtt

import (
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"sync"
	"time"
)

const (
	packetConnect  = 0x10
	packetConnAck  = 0x20
	packetPublish  = 0x30
	packetPingReq  = 0xC0
	dialTimeout    = 5 * time.Second
	writeTimeout   = 5 * time.Second
	reconnectDelay = 2 * time.Second
)

// Client publishes to one broker and reconnects on the next Publish after a failure.
type Client struct {
	network   string // "tcp" or "tls"
	addr      string
	clientID  string
	username  string
	password  string
	keepAlive time.Duration

	mu        sync.Mutex
	conn      net.Conn
	lastWrite time.Time
	lastDial  time.Time
}

// New accepts mqtt://host:port and mqtts://host:port, with optional user:password@.
func New(rawURL, clientID string) (*Client, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("mqtt url: %w", err)
	}
	c := &Client{clientID: clientID, keepAlive: 30 * time.Second}
	port := u.Port()
	switch u.Scheme {
	case "mqtt", "tcp":
		c.network = "tcp"
		if port == "" {
			port = "1883"
		}
	case "mqtts", "tls", "ssl":
		c.network = "tls"
		if port == "" {
			port = "8883"
		}
	default:
		return nil, fmt.Errorf("mqtt url: unsupported scheme %q (use mqtt:// or mqtts://)", u.Scheme)
	}
	if u.Hostname() == "" {
		return nil, errors.New("mqtt url: missing host")
	}
	c.addr = net.JoinHostPort(u.Hostname(), port)
	if u.User != nil {
		c.username = u.User.Username()
		c.password, _ = u.User.Password()
	}
	return c, nil
}

// Publish sends one QoS 0 message, connecting first if needed.
func (c *Client) Publish(topic string, payload []byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := c.ensureConnected(); err != nil {
		return err
	}
	body := appendString(nil, topic)
	body = append(body, payload...)
	return c.write(packet(packetPublish, body))
}

// KeepAlive pings the broker when nothing has been sent for a while. Call it
// periodically; it does nothing while disconnected or while publishes are flowing.
func (c *Client) KeepAlive() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conn != nil && time.Since(c.lastWrite) > c.keepAlive/2 {
		_ = c.write([]byte{packetPingReq, 0})
	}
}

// Close drops the connection without the MQTT DISCONNECT handshake.
func (c *Client) Close() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.drop(c.conn)
}

func (c *Client) ensureConnected() error {
	if c.conn != nil {
		return nil
	}
	if time.Since(c.lastDial) < reconnectDelay {
		return fmt.Errorf("mqtt: not connected to %s, retrying every %s", c.addr, reconnectDelay)
	}
	c.lastDial = time.Now()

	var conn net.Conn
	var err error
	dialer := &net.Dialer{Timeout: dialTimeout}
	if c.network == "tls" {
		conn, err = tls.DialWithDialer(dialer, "tcp", c.addr, &tls.Config{MinVersion: tls.VersionTLS12})
	} else {
		conn, err = dialer.Dial("tcp", c.addr)
	}
	if err != nil {
		return fmt.Errorf("mqtt: %w", err)
	}

	// CONNECT: protocol name and level 4 (3.1.1), clean session, keep-alive, client id.
	flags := byte(0x02)
	body := appendString(nil, "MQTT")
	body = append(body, 4, 0, byte(int(c.keepAlive.Seconds())>>8), byte(int(c.keepAlive.Seconds())))
	body = appendString(body, c.clientID)
	if c.username != "" {
		flags |= 0x80
		body = appendString(body, c.username)
		if c.password != "" {
			flags |= 0x40
			body = appendString(body, c.password)
		}
	}
	body[7] = flags

	_ = conn.SetDeadline(time.Now().Add(dialTimeout))
	ack := make([]byte, 4)
	if _, err = conn.Write(packet(packetConnect, body)); err == nil {
		_, err = io.ReadFull(conn, ack)
	}
	if err == nil && (ack[0] != packetConnAck || ack[1] != 2) {
		err = fmt.Errorf("unexpected reply to CONNECT: % x", ack)
	}
	if err == nil && ack[3] != 0 {
		err = fmt.Errorf("broker refused the connection, return code %d", ack[3])
	}
	if err != nil {
		conn.Close()
		return fmt.Errorf("mqtt: connect to %s: %w", c.addr, err)
	}
	_ = conn.SetDeadline(time.Time{})

	c.conn, c.lastWrite = conn, time.Now()
	go c.drain(conn)
	return nil
}

// drain reads and discards what the broker sends (ping responses). Its real job
// is to notice a closed connection promptly, so the next Publish reconnects
// instead of writing into a dead socket.
func (c *Client) drain(conn net.Conn) {
	_, _ = io.Copy(io.Discard, conn)
	c.mu.Lock()
	defer c.mu.Unlock()
	c.drop(conn)
}

func (c *Client) write(p []byte) error {
	_ = c.conn.SetWriteDeadline(time.Now().Add(writeTimeout))
	if _, err := c.conn.Write(p); err != nil {
		c.drop(c.conn)
		return fmt.Errorf("mqtt: %w", err)
	}
	c.lastWrite = time.Now()
	return nil
}

// drop closes conn and, if it is still the current connection, forgets it.
func (c *Client) drop(conn net.Conn) {
	if conn == nil {
		return
	}
	conn.Close()
	if c.conn == conn {
		c.conn = nil
	}
}

func packet(header byte, body []byte) []byte {
	out := []byte{header}
	n := len(body)
	for { // remaining length, 7 bits per byte, low bits first
		b := byte(n % 128)
		n /= 128
		if n > 0 {
			b |= 0x80
		}
		out = append(out, b)
		if n == 0 {
			break
		}
	}
	return append(out, body...)
}

func appendString(b []byte, s string) []byte {
	b = append(b, byte(len(s)>>8), byte(len(s)))
	return append(b, s...)
}
