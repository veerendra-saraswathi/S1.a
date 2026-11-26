// Package input opens the link the gateway reads MAVLink from: a UDP port, a TCP
// connection, or a serial device such as a flight controller's USB port or a
// telemetry radio.
//
// Every link is opened for reading only. A Link has no Write method, the serial
// device is opened read-only, and nothing is ever written to a TCP connection, so
// the gateway cannot command a vehicle whichever link it uses.
package input

import (
	"errors"
	"fmt"
	"net"
	"strconv"
	"strings"
)

// Kind is the type of link.
type Kind int

const (
	UDP Kind = iota
	TCP
	Serial
)

func (k Kind) String() string {
	return [...]string{"udp", "tcp", "serial"}[k]
}

// DefaultBaud suits a USB connection, where the rate is ignored, and most
// flight-controller telemetry ports. SiK radios default to 57600.
const DefaultBaud = 115200

// Spec says which link to open.
type Spec struct {
	Kind Kind
	Addr string // host:port for UDP and TCP, the device path for serial
	Baud int    // serial only
}

// Parse reads a link spec:
//
//	udp::14550                       listen on UDP 14550, all interfaces
//	udp:127.0.0.1:14550              listen on one address
//	tcp:127.0.0.1:5763               connect to a TCP server, such as SITL or MAVProxy
//	serial:/dev/cu.usbmodem01        a serial device at 115200 baud
//	serial:/dev/ttyUSB0:57600        a serial device at a given rate
func Parse(spec string) (Spec, error) {
	kind, rest, ok := strings.Cut(spec, ":")
	if !ok || rest == "" {
		return Spec{}, fmt.Errorf("%q: want udp:<addr>, tcp:<host:port> or serial:<device>[:<baud>]", spec)
	}
	switch kind {
	case "udp", "tcp":
		host, port, err := net.SplitHostPort(rest)
		if err != nil {
			return Spec{}, fmt.Errorf("%q: %v", spec, err)
		}
		if n, err := strconv.Atoi(port); err != nil || n < 1 || n > 65535 {
			return Spec{}, fmt.Errorf("%q: port must be 1-65535", spec)
		}
		if kind == "tcp" && host == "" {
			return Spec{}, fmt.Errorf("%q: a TCP link needs a host to connect to", spec)
		}
		k := UDP
		if kind == "tcp" {
			k = TCP
		}
		return Spec{Kind: k, Addr: rest}, nil
	case "serial":
		path, baud := rest, DefaultBaud
		if i := strings.LastIndex(rest, ":"); i >= 0 {
			n, err := strconv.Atoi(rest[i+1:])
			if err != nil {
				return Spec{}, fmt.Errorf("%q: baud rate %q is not a number", spec, rest[i+1:])
			}
			path, baud = rest[:i], n
		}
		if !strings.HasPrefix(path, "/") {
			return Spec{}, fmt.Errorf("%q: the device must be an absolute path, such as /dev/ttyACM0", spec)
		}
		if _, err := speed(baud); err != nil {
			return Spec{}, fmt.Errorf("%q: %v", spec, err)
		}
		return Spec{Kind: Serial, Addr: path, Baud: baud}, nil
	}
	return Spec{}, fmt.Errorf("%q: unknown link %q; want udp, tcp or serial", spec, kind)
}

func (s Spec) String() string {
	if s.Kind == Serial {
		return fmt.Sprintf("serial %s at %d baud", s.Addr, s.Baud)
	}
	return s.Kind.String() + " " + s.Addr
}

// Datagram reports whether each Read returns whole frames, as UDP does. For a
// byte stream a frame can be split across reads, so the parser must keep state.
func (s Spec) Datagram() bool { return s.Kind == UDP }

// Link is an open input. Close unblocks a Read in progress.
type Link interface {
	Read(p []byte) (int, error)
	Close() error
}

// Open opens the link once. Callers retry a stream link that fails or closes.
func (s Spec) Open() (Link, error) {
	switch s.Kind {
	case UDP:
		conn, err := net.ListenPacket("udp", s.Addr)
		if err != nil {
			return nil, err
		}
		return udpLink{conn}, nil
	case TCP:
		conn, err := net.Dial("tcp", s.Addr)
		if err != nil {
			return nil, err
		}
		return readOnly{conn}, nil
	case Serial:
		return openSerial(s.Addr, s.Baud)
	}
	return nil, errors.New("unknown link kind")
}

type udpLink struct{ conn net.PacketConn }

func (l udpLink) Read(p []byte) (int, error) {
	n, _, err := l.conn.ReadFrom(p)
	return n, err
}

func (l udpLink) Close() error { return l.conn.Close() }

// readOnly hides a connection's Write method.
type readOnly struct{ conn net.Conn }

func (r readOnly) Read(p []byte) (int, error) { return r.conn.Read(p) }
func (r readOnly) Close() error               { return r.conn.Close() }
