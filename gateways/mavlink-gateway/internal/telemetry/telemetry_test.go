package telemetry

import (
	"bufio"
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/veerendra-saraswathi/S1.a/gateways/mavlink-gateway/internal/mavlink"
)

const tenant = "00000000-0000-0000-0000-000000000001"

func feed(t *testing.T, a *Aggregator, names ...string) {
	t.Helper()
	file, err := os.Open("../mavlink/testdata/golden.txt")
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
	var p mavlink.Parser
	for _, name := range names {
		raw, ok := frames[name]
		if !ok {
			t.Fatalf("no golden frame %q", name)
		}
		for _, f := range p.Write(raw) {
			a.Apply(f)
		}
	}
}

func TestFlushBuildsContractMessage(t *testing.T) {
	a := NewAggregator(Config{TenantID: tenant, ObjectType: "CommercialDrone", Vehicles: map[uint8]string{7: "DRN-0007"}})
	feed(t, a, "heartbeat_v2", "sys_status_v2", "attitude_v2", "global_position_int_v2", "vfr_hud_v2",
		"gps_raw_int_v2", "battery_status_v2")

	now := time.Date(2026, 11, 19, 1, 2, 3, 456_000_000, time.FixedZone("IST", 19800))
	out := a.Flush(now)
	if len(out) != 1 {
		t.Fatalf("got %d messages, want 1", len(out))
	}
	if want := "s2/v1/" + tenant + "/CommercialDrone/DRN-0007/telemetry"; out[0].Topic != want {
		t.Errorf("topic = %s, want %s", out[0].Topic, want)
	}

	raw, err := json.Marshal(out[0].Message)
	if err != nil {
		t.Fatal(err)
	}
	var got struct {
		TS       string
		Seq      int
		Metrics  map[string]any
		Position map[string]float64
	}
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	if got.TS != "2026-11-18T19:32:03.456Z" || got.Seq != 1 {
		t.Errorf("ts=%s seq=%d", got.TS, got.Seq)
	}
	if got.Metrics["armed"] != true || got.Metrics["system_state"] != "ACTIVE" {
		t.Errorf("heartbeat metrics = %v", got.Metrics)
	}
	for name, want := range map[string]float64{
		"battery_voltage": 24.31, "battery_current_a": 12.5, "battery_remaining_pct": 76, "cpu_load_pct": 23.5,
		"rel_alt_m": 40.25, "climb_rate_mps": 1.5, "throttle_pct": 55, "gps_fix_type": 3, "gps_satellites": 14,
		"battery_temperature_c": 31.5, "custom_mode": 3,
	} {
		if v, ok := got.Metrics[name].(float64); !ok || v < want-1e-4 || v > want+1e-4 {
			t.Errorf("metric %s = %v, want %v", name, got.Metrics[name], want)
		}
	}
	for name, want := range map[string]float64{"lat": 17.4401, "lon": 78.3489, "alt_m": 580.5, "heading_deg": 87, "speed_mps": 5} {
		if v := got.Position[name]; v < want-1e-4 || v > want+1e-4 {
			t.Errorf("position.%s = %v, want %v", name, v, want)
		}
	}

	if again := a.Flush(now); len(again) != 0 {
		t.Errorf("a second flush with no new frames published %d messages", len(again))
	}
	feed(t, a, "attitude_v2")
	if next := a.Flush(now); len(next) != 1 || next[0].Message.Seq != 2 || next[0].Message.Position != nil {
		t.Errorf("flush after one attitude frame = %+v", next)
	}
}

func TestIgnoresGroundStationsAndMissingFix(t *testing.T) {
	a := NewAggregator(Config{TenantID: tenant, ObjectType: "CommercialDrone"})
	feed(t, a, "gcs_heartbeat_v2")
	if out := a.Flush(time.Now()); len(out) != 0 {
		t.Fatalf("a ground station heartbeat produced telemetry: %+v", out)
	}
	feed(t, a, "global_position_no_heading_v2", "sys_status_unknown_v2")
	out := a.Flush(time.Now())
	if len(out) != 1 || out[0].Message.Position != nil {
		t.Fatalf("a 0,0 position must not be published: %+v", out)
	}
	if !strings.HasSuffix(out[0].Topic, "/CommercialDrone/MAV-7/telemetry") {
		t.Errorf("unmapped system topic = %s", out[0].Topic)
	}
	if _, ok := out[0].Message.Metrics["battery_voltage"]; ok {
		t.Errorf("unknown battery voltage was published: %v", out[0].Message.Metrics)
	}
}
