package input

import (
	"bytes"
	"io"
	"net"
	"strings"
	"testing"
	"time"
)

func TestParse(t *testing.T) {
	good := map[string]Spec{
		"udp::14550":                {Kind: UDP, Addr: ":14550"},
		"udp:127.0.0.1:14550":       {Kind: UDP, Addr: "127.0.0.1:14550"},
		"tcp:127.0.0.1:5763":        {Kind: TCP, Addr: "127.0.0.1:5763"},
		"tcp:[::1]:5760":            {Kind: TCP, Addr: "[::1]:5760"},
		"serial:/dev/cu.usbmodem01": {Kind: Serial, Addr: "/dev/cu.usbmodem01", Baud: DefaultBaud},
		"serial:/dev/ttyUSB0:57600": {Kind: Serial, Addr: "/dev/ttyUSB0", Baud: 57600},
	}
	for in, want := range good {
		got, err := Parse(in)
		if err != nil || got != want {
			t.Errorf("Parse(%q) = %+v, %v; want %+v", in, got, err, want)
		}
	}
	bad := map[string]string{
		"":                         "want udp",
		"14550":                    "want udp",
		"udp:14550":                "missing port",
		"udp::0":                   "port must be",
		"tcp::5763":                "needs a host",
		"serial:ttyUSB0":           "absolute path",
		"serial:/dev/ttyUSB0:fast": "not a number",
		"serial:/dev/ttyUSB0:-1":   "baud rate",
		"ws:host:1":                "unknown link",
	}
	for in, want := range bad {
		if _, err := Parse(in); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("Parse(%q) error = %v, want one containing %q", in, err, want)
		}
	}
}

func TestTCPLinkReadsAndHasNoWrite(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	sent := []byte{0xfd, 0x09, 0x00, 0x00, 0x01}
	go func() {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		c.Write(sent)
		c.Close()
	}()
	link, err := Spec{Kind: TCP, Addr: ln.Addr().String()}.Open()
	if err != nil {
		t.Fatal(err)
	}
	defer link.Close()
	if _, ok := link.(io.Writer); ok {
		t.Fatal("a TCP link must not be writable")
	}
	got, err := io.ReadAll(link)
	if err != nil || !bytes.Equal(got, sent) {
		t.Fatalf("read %x, %v; want %x", got, err, sent)
	}
}

func TestCloseUnblocksRead(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() { // accept and then stay silent
		if c, err := ln.Accept(); err == nil {
			defer c.Close()
			time.Sleep(5 * time.Second)
		}
	}()
	link, err := Spec{Kind: TCP, Addr: ln.Addr().String()}.Open()
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { _, err := link.Read(make([]byte, 64)); done <- err }()
	time.Sleep(50 * time.Millisecond)
	link.Close()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("Read returned no error after Close")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Close did not unblock Read")
	}
}
