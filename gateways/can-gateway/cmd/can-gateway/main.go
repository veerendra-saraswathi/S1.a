// can-gateway reads a DroneCAN bus capture and republishes it over MQTT in the
// S2 telemetry contract.
//
//	candump -L can0 | can-gateway -nodes 10=SUB-DRN1-GIMBAL       live, on the bench PC
//	can-gateway -input bench-run.log -nodes 10=SUB-DRN1-GIMBAL     a recorded run
//
// It reads `candump -L` lines, so the capture is can-utils' and the timestamps
// are the capturing kernel's. It never writes to the bus: it has no CAN socket
// at all. This is the soft real-time path; nothing in a bench control loop
// should wait on it.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/veerendra-saraswathi/S1.a/gateways/can-gateway/internal/bus"
	"github.com/veerendra-saraswathi/S1.a/gateways/internal/mqtt"
	"github.com/veerendra-saraswathi/S1.a/gateways/internal/s2"
)

var (
	uuidPattern   = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
	pascalPattern = regexp.MustCompile(`^[A-Z][A-Za-z0-9]{0,62}$`)
)

func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

func main() {
	input := flag.String("input", "-", "candump -L log to read; - is stdin")
	iface := flag.String("iface", "", "only frames from this interface, e.g. can0 (default: all)")
	mqttURL := flag.String("mqtt", env("S2_MQTT_URL", "mqtt://localhost:1883"), "MQTT broker URL")
	tenant := flag.String("tenant", env("S2_TENANT", "00000000-0000-0000-0000-000000000001"),
		"S2 tenant UUID (the default is S2's local dev tenant)")
	objectType := flag.String("object-type", env("CAN_GATEWAY_OBJECT_TYPE", "Subsystem"), "S2 object type of the nodes")
	nodes := flag.String("nodes", env("CAN_GATEWAY_NODES", ""),
		"node id to S2 primary key, e.g. 10=SUB-DRN1-GIMBAL (unmapped nodes publish as CAN-<id>)")
	actuators := flag.String("actuators", env("CAN_GATEWAY_ACTUATORS", ""),
		"actuator id to S2 primary key, overriding the node that reports it")
	rateHz := flag.Float64("rate-hz", 10, "messages per object per second of capture time")
	dryRun := flag.Bool("dry-run", false, "print 'topic payload' lines to stdout instead of publishing")
	clientID := flag.String("client-id", env("CAN_GATEWAY_CLIENT_ID", "can-gateway"), "MQTT client id (unique per gateway)")
	flag.Parse()

	if !uuidPattern.MatchString(*tenant) {
		log.Fatalf("-tenant must be a UUID, got %q", *tenant)
	}
	if !pascalPattern.MatchString(*objectType) {
		log.Fatalf("-object-type must be PascalCase, got %q", *objectType)
	}
	if *rateHz <= 0 || *rateHz > 100 {
		log.Fatalf("-rate-hz must be in (0, 100], got %v", *rateHz)
	}
	nodeMap, err := parseIDs(*nodes, 1, 127)
	if err != nil {
		log.Fatalf("-nodes: %v", err)
	}
	actuatorMap, err := parseIDs(*actuators, 0, 255)
	if err != nil {
		log.Fatalf("-actuators: %v", err)
	}

	var in io.Reader = os.Stdin
	if *input != "-" {
		file, err := os.Open(*input)
		if err != nil {
			log.Fatal(err)
		}
		defer file.Close()
		in = file
	}

	var client *mqtt.Client
	if !*dryRun {
		if client, err = mqtt.New(*mqttURL, *clientID); err != nil {
			log.Fatal(err)
		}
		defer client.Close()
		go func() { // a quiet capture must not let the broker drop us
			for range time.Tick(5 * time.Second) {
				client.KeepAlive()
			}
		}()
	}

	p := bus.New(bus.Config{
		TenantID: strings.ToLower(*tenant), ObjectType: *objectType, Nodes: nodeMap, Actuators: actuatorMap,
		Interval: time.Duration(float64(time.Second) / *rateHz), Iface: *iface,
	})
	source := *input
	if source == "-" {
		source = "stdin"
	}
	log.Printf("reading candump -L from %s, %g messages per object per second of capture (%s)", source, *rateHz, target(*dryRun, *mqttURL))

	var failed uint64
	var lastErr string
	emit := func(out s2.Outbound) {
		payload, err := json.Marshal(out.Message)
		if err == nil {
			if *dryRun {
				_, err = fmt.Printf("%s %s\n", out.Topic, payload)
			} else {
				err = client.Publish(out.Topic, payload)
			}
		}
		if err != nil {
			failed++
			if msg := err.Error(); msg != lastErr { // log a failure once, not once per message
				log.Printf("publish failed: %v", err)
				lastErr = msg
			}
			return
		}
		if lastErr != "" {
			log.Printf("publishing again")
			lastErr = ""
		}
	}

	lines := make(chan string, 1024)
	go func() {
		scanner := bufio.NewScanner(in)
		for scanner.Scan() {
			lines <- scanner.Text()
		}
		if err := scanner.Err(); err != nil {
			log.Printf("read: %v", err)
		}
		close(lines)
	}()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	report := time.NewTicker(30 * time.Second)
	defer report.Stop()

	for {
		select {
		case line, ok := <-lines:
			if !ok {
				p.Flush(emit)
				summary(p, failed, "end of input")
				return
			}
			p.Line(line, emit)
		case <-report.C:
			summary(p, failed, "")
		case <-ctx.Done():
			p.Flush(emit)
			summary(p, failed, "stopping")
			return
		}
	}
}

func summary(p *bus.Pipeline, failed uint64, why string) {
	t, s := p.Transport(), p.Stats
	prefix := ""
	if why != "" {
		prefix = why + ": "
	}
	log.Printf("%slines=%d bad_lines=%d unsupported=%d other_iface=%d | frames=%d transfers=%d undecoded=%d ignored=%d "+
		"malformed=%d bad_crc=%d timed_out=%d bad_payload=%d unattributed_commands=%d | published=%d failed=%d",
		prefix, s.Lines, s.BadLines, s.Unsupported, s.OtherIface, t.FrameCount, t.Transfers, t.Undecoded, t.Ignored,
		t.Malformed, t.BadCRC, t.TimedOut, s.BadPayload, s.Unattributed, s.Published-failed, failed)
}

func target(dryRun bool, mqttURL string) string {
	if dryRun {
		return "dry run, stdout"
	}
	if i := strings.LastIndex(mqttURL, "@"); i >= 0 { // never log credentials
		mqttURL = mqttURL[:strings.Index(mqttURL, "://")+3] + mqttURL[i+1:]
	}
	return "to " + mqttURL
}

func parseIDs(spec string, lo, hi uint64) (map[uint8]string, error) {
	out := make(map[uint8]string)
	if strings.TrimSpace(spec) == "" {
		return out, nil
	}
	for _, pair := range strings.Split(spec, ",") {
		id, key, ok := strings.Cut(strings.TrimSpace(pair), "=")
		if !ok {
			return nil, fmt.Errorf("%q is not <id>=<primary key>", pair)
		}
		n, err := strconv.ParseUint(id, 10, 8)
		if err != nil || n < lo || n > hi {
			return nil, fmt.Errorf("%q: id must be %d-%d", pair, lo, hi)
		}
		if key == "" || strings.ContainsAny(key, "/+# \t") {
			return nil, fmt.Errorf("%q: primary key must be non-empty without '/', '+', '#' or spaces", pair)
		}
		out[uint8(n)] = key
	}
	return out, nil
}
