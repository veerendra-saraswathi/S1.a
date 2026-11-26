package main

import (
	"bufio"
	"context"
	"encoding/hex"
	"net"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/veerendra-saraswathi/S1.a/gateways/internal/s2/s2test"
	"github.com/veerendra-saraswathi/S1.a/gateways/mavlink-gateway/internal/input"
	"github.com/veerendra-saraswathi/S1.a/gateways/mavlink-gateway/internal/mavlink"
	"github.com/veerendra-saraswathi/S1.a/gateways/mavlink-gateway/internal/telemetry"
)

func golden(t *testing.T, names ...string) []byte {
	t.Helper()
	file, err := os.Open("../../internal/mavlink/testdata/golden.txt")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	frames := make(map[string][]byte)
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		if name, encoded, ok := strings.Cut(scanner.Text(), " "); ok && !strings.HasPrefix(name, "#") {
			frames[name], _ = hex.DecodeString(encoded)
		}
	}
	var out []byte
	for _, name := range names {
		raw, ok := frames[name]
		if !ok {
			t.Fatalf("no golden frame %q", name)
		}
		out = append(out, raw...)
	}
	return out
}

// A TCP link that drops mid-frame is reopened, and the half frame from the first
// connection is not joined to the bytes of the second.
func TestReceiveReconnectsAStreamLink(t *testing.T) {
	retryInterval = 50 * time.Millisecond
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()

	first := golden(t, "heartbeat_v2", "attitude_v2")
	cut := len(first) - 5 // the attitude frame loses its last 5 bytes with the connection
	second := golden(t, "heartbeat_v2", "global_position_int_v2", "sys_status_v2")
	go func() {
		for i, chunk := range [][]byte{first[:cut], second} {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			for j := 0; j < len(chunk); j += 7 { // a stream arrives in arbitrary pieces
				c.Write(chunk[j:min(j+7, len(chunk))])
				time.Sleep(time.Millisecond)
			}
			if i == 0 {
				c.Close()
			} else {
				defer c.Close()
				time.Sleep(2 * time.Second) // stay connected while the test reads
			}
		}
	}()

	agg := telemetry.NewAggregator(telemetry.Config{TenantID: "00000000-0000-0000-0000-000000000001",
		ObjectType: "CommercialDrone", Vehicles: map[uint8]string{7: "DRN-0007"}})
	var stats atomic.Pointer[mavlink.Stats]
	stats.Store(&mavlink.Stats{})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		receive(ctx, input.Spec{Kind: input.TCP, Addr: ln.Addr().String()}, nil, agg, &stats)
		close(done)
	}()

	deadline := time.Now().Add(3 * time.Second)
	for stats.Load().Frames < 4 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("receive did not stop when its context ended")
	}

	s := stats.Load()
	if s.Frames != 4 || s.BadCRC != 0 { // 1 heartbeat, then 3 frames after the reconnect
		t.Fatalf("stats %+v; want 4 frames and no bad CRC", *s)
	}
	out := agg.Flush(time.Now())
	s2test.Record(t, out...)
	if len(out) != 1 || out[0].Message.Position == nil {
		t.Fatalf("flush = %+v; want one message with the position from the second connection", out)
	}
}

// A port proxy with nothing behind it accepts and at once closes. The loop must
// wait between attempts rather than spin.
func TestReceiveBacksOffFromALinkThatClosesAtOnce(t *testing.T) {
	retryInterval = 50 * time.Millisecond
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	var accepts atomic.Int64
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			accepts.Add(1)
			c.Close()
		}
	}()
	agg := telemetry.NewAggregator(telemetry.Config{TenantID: "00000000-0000-0000-0000-000000000001", ObjectType: "CommercialDrone"})
	var stats atomic.Pointer[mavlink.Stats]
	stats.Store(&mavlink.Stats{})
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	receive(ctx, input.Spec{Kind: input.TCP, Addr: ln.Addr().String()}, nil, agg, &stats)
	if n := accepts.Load(); n < 2 || n > 12 { // about 10 in 500 ms at a 50 ms interval
		t.Fatalf("%d connections in 500 ms; want one per retry interval", n)
	}
}
