// mavlink-gateway listens for MAVLink on UDP and republishes it over MQTT in the
// S2 telemetry contract. Point an autopilot, SITL or a telemetry radio's UDP
// output at it; S2's mqtt-bridge picks the messages up from the broker.
//
//	MAVLink (UDP) -> mavlink-gateway -> MQTT -> S2 mqtt-bridge -> Kafka -> TimescaleDB
//
// This is the soft real-time path. Nothing in a flight-control or bench control
// loop should wait on it.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/veerendra-saraswathi/S1.a/gateways/mavlink-gateway/internal/mavlink"
	"github.com/veerendra-saraswathi/S1.a/gateways/mavlink-gateway/internal/mqtt"
	"github.com/veerendra-saraswathi/S1.a/gateways/mavlink-gateway/internal/telemetry"
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
	listen := flag.String("listen", env("GATEWAY_LISTEN", ":14550"), "UDP address to receive MAVLink on")
	mqttURL := flag.String("mqtt", env("S2_MQTT_URL", "mqtt://localhost:1883"), "MQTT broker URL")
	tenant := flag.String("tenant", env("S2_TENANT", "00000000-0000-0000-0000-000000000001"),
		"S2 tenant UUID (the default is S2's local dev tenant)")
	objectType := flag.String("object-type", env("GATEWAY_OBJECT_TYPE", "CommercialDrone"), "S2 object type of the vehicles")
	vehicles := flag.String("vehicles", env("GATEWAY_VEHICLES", ""),
		"MAVLink system id to S2 primary key, e.g. 1=DRN-0001,2=DRN-0002 (unmapped systems publish as MAV-<id>)")
	rateHz := flag.Float64("rate-hz", 1, "messages published per vehicle per second")
	dryRun := flag.Bool("dry-run", false, "print 'topic payload' lines to stdout instead of publishing")
	clientID := flag.String("client-id", env("GATEWAY_CLIENT_ID", "mavlink-gateway"), "MQTT client id (unique per gateway)")
	flag.Parse()

	if !uuidPattern.MatchString(*tenant) {
		log.Fatalf("-tenant must be a UUID, got %q", *tenant)
	}
	if !pascalPattern.MatchString(*objectType) {
		log.Fatalf("-object-type must be PascalCase, got %q", *objectType)
	}
	if *rateHz <= 0 || *rateHz > 50 {
		log.Fatalf("-rate-hz must be in (0, 50], got %v", *rateHz)
	}
	vehicleMap, err := parseVehicles(*vehicles)
	if err != nil {
		log.Fatalf("-vehicles: %v", err)
	}

	var client *mqtt.Client
	if !*dryRun {
		if client, err = mqtt.New(*mqttURL, *clientID); err != nil {
			log.Fatal(err)
		}
		defer client.Close()
	}

	conn, err := net.ListenPacket("udp", *listen)
	if err != nil {
		log.Fatalf("listen: %v", err)
	}
	log.Printf("listening for MAVLink on udp %s, publishing at %g Hz (%s)", conn.LocalAddr(), *rateHz, target(*dryRun, *mqttURL))

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() { <-ctx.Done(); conn.Close() }()

	agg := telemetry.NewAggregator(telemetry.Config{TenantID: strings.ToLower(*tenant), ObjectType: *objectType, Vehicles: vehicleMap})
	var stats atomic.Pointer[mavlink.Stats]
	stats.Store(&mavlink.Stats{})

	go func() { // receive loop: the only goroutine that touches the parser
		var parser mavlink.Parser
		buf := make([]byte, 65535)
		for {
			n, _, err := conn.ReadFrom(buf)
			if err != nil {
				if !errors.Is(err, net.ErrClosed) {
					log.Printf("udp read: %v", err)
				}
				return
			}
			parser.Reset() // a datagram holds whole frames; never join two datagrams
			for _, frame := range parser.Write(buf[:n]) {
				agg.Apply(frame)
			}
			snapshot := parser.Stats
			stats.Store(&snapshot)
		}
	}()

	publish := time.NewTicker(time.Duration(float64(time.Second) / *rateHz))
	defer publish.Stop()
	report := time.NewTicker(30 * time.Second)
	defer report.Stop()
	var published, failed uint64
	var lastErr string

	for {
		select {
		case <-ctx.Done():
			log.Printf("stopping: published %d, failed %d", published, failed)
			return
		case now := <-publish.C:
			for _, out := range agg.Flush(now) {
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
					continue
				}
				published++
				if lastErr != "" {
					log.Printf("publishing again")
					lastErr = ""
				}
			}
			if client != nil {
				client.KeepAlive()
			}
		case <-report.C:
			s := stats.Load()
			log.Printf("mavlink frames=%d bad_crc=%d undecoded=%d dropped=%d garbage_bytes=%d | mqtt published=%d failed=%d",
				s.Frames, s.BadCRC, s.Unknown, s.Dropped, s.Garbage, published, failed)
		}
	}
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

func parseVehicles(spec string) (map[uint8]string, error) {
	out := make(map[uint8]string)
	if strings.TrimSpace(spec) == "" {
		return out, nil
	}
	for _, pair := range strings.Split(spec, ",") {
		id, key, ok := strings.Cut(strings.TrimSpace(pair), "=")
		if !ok {
			return nil, fmt.Errorf("%q is not <system id>=<primary key>", pair)
		}
		n, err := strconv.ParseUint(id, 10, 8)
		if err != nil || n == 0 {
			return nil, fmt.Errorf("%q: system id must be 1-255", pair)
		}
		if key == "" || strings.ContainsAny(key, "/+# \t") {
			return nil, fmt.Errorf("%q: primary key must be non-empty without '/', '+', '#' or spaces", pair)
		}
		out[uint8(n)] = key
	}
	return out, nil
}
