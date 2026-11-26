package bus

import (
	"bufio"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/veerendra-saraswathi/S1.a/gateways/internal/s2"
	"github.com/veerendra-saraswathi/S1.a/gateways/internal/s2/s2test"
)

const tenant = "00000000-0000-0000-0000-000000000001"

func goldenLines(t testing.TB) []string {
	t.Helper()
	file, err := os.Open("../dronecan/testdata/golden.log")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	var out []string
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		if name, line, ok := strings.Cut(scanner.Text(), " "); ok && !strings.HasPrefix(name, "#") {
			out = append(out, line)
		}
	}
	return out
}

// run feeds lines through p, and hands what it published to the contract check (s2test).
func run(t testing.TB, p *Pipeline, lines []string) []s2.Outbound {
	t.Helper()
	var out []s2.Outbound
	emit := func(o s2.Outbound) { out = append(out, o) }
	for _, l := range lines {
		p.Line(l, emit)
	}
	p.Flush(emit)
	s2test.Record(t, out...)
	return out
}

func byKey(out []s2.Outbound) map[string]s2.Message {
	m := make(map[string]s2.Message)
	for _, o := range out {
		parts := strings.Split(o.Topic, "/")
		m[parts[4]] = o.Message
	}
	return m
}

func TestGoldenCaptureBecomesContractMessages(t *testing.T) {
	p := New(Config{
		TenantID: tenant, ObjectType: "Subsystem", Interval: time.Second,
		Nodes: map[uint8]string{10: "SUB-DRN1-GIMBAL"},
	})
	out := run(t, p, goldenLines(t))
	msgs := byKey(out)
	if len(out) != 2 {
		t.Fatalf("got %d messages, want 2 (gimbal node, ESC node): %+v", len(out), out)
	}
	if want := "s2/v1/" + tenant + "/Subsystem/SUB-DRN1-GIMBAL/telemetry"; out[1].Topic != want && out[0].Topic != want {
		t.Errorf("topics = %s, %s", out[0].Topic, out[1].Topic)
	}

	gimbal := msgs["SUB-DRN1-GIMBAL"]
	for name, want := range map[string]any{
		"node_health": "WARNING", "node_mode": "MAINTENANCE", "node_uptime_s": 1234.0,
		"actuator_3_position": 0.25, "actuator_3_force": -1.5, "actuator_3_speed": 2.0, "actuator_3_power_pct": 40.0,
		"actuator_4_position": -0.5,
		// The commands came from node 1 but are filed with the actuators they command.
		"actuator_3_command": 0.5, "actuator_3_command_type": "POSITION", "actuator_4_command": -0.25,
		"clock_source": "capture",
	} {
		if got := gimbal.Metrics[name]; fmt.Sprint(got) != fmt.Sprint(want) {
			t.Errorf("gimbal %s = %v, want %v", name, got, want)
		}
	}
	if _, ok := gimbal.Metrics["actuator_4_force"]; ok {
		t.Error("an unmeasured force (NaN) was published")
	}
	// Stamped with the newest frame that touched it: the command, at .010.
	if gimbal.TS != "2026-11-25T09:46:40.010Z" {
		t.Errorf("gimbal ts = %s", gimbal.TS)
	}

	esc := msgs["CAN-20"] // unmapped node
	for name, want := range map[string]float64{
		"esc_2_rpm": -12345, "esc_2_voltage": 24.5, "esc_2_current_a": 12.25, "esc_2_power_pct": 55, "esc_2_error_count": 7,
	} {
		if got, _ := esc.Metrics[name].(float64); got != want {
			t.Errorf("esc %s = %v, want %v", name, esc.Metrics[name], want)
		}
	}
	if c, _ := esc.Metrics["esc_2_temperature_c"].(float64); c < 39.8 || c > 40.2 {
		t.Errorf("esc temperature = %v °C, want 40 (313.15 K in float16)", c)
	}

	for _, o := range out {
		for name := range o.Message.Metrics {
			if !s2.ValidMetric(name) {
				t.Errorf("metric %q is not a valid S2 metric name", name)
			}
		}
	}
	tr := p.Transport()
	if tr.Undecoded != 1 || tr.Ignored != 2 || tr.BadCRC != 0 || p.Stats.BadLines != 0 {
		t.Errorf("transport %+v, pipeline %+v", tr, p.Stats)
	}
}

func TestActuatorMapOverridesTheReportingNode(t *testing.T) {
	p := New(Config{
		TenantID: tenant, ObjectType: "Subsystem", Interval: time.Second,
		Nodes: map[uint8]string{10: "SUB-DRN1-GIMBAL"}, Actuators: map[uint8]string{4: "SUB-DRN1-PAYLOAD"},
	})
	msgs := byKey(run(t, p, goldenLines(t)))
	if _, ok := msgs["SUB-DRN1-PAYLOAD"].Metrics["actuator_4_position"]; !ok {
		t.Errorf("actuator 4 should be filed under SUB-DRN1-PAYLOAD: %v", msgs)
	}
	if _, ok := msgs["SUB-DRN1-GIMBAL"].Metrics["actuator_4_position"]; ok {
		t.Error("actuator 4 also landed on the reporting node")
	}
}

func TestACommandBeforeAnyStatusIsCountedNotGuessed(t *testing.T) {
	lines := goldenLines(t)
	var commandsOnly []string
	for _, l := range lines {
		if strings.Contains(l, "1003F201#") {
			commandsOnly = append(commandsOnly, l)
		}
	}
	p := New(Config{TenantID: tenant, ObjectType: "Subsystem", Interval: time.Second})
	if out := run(t, p, commandsOnly); len(out) != 0 {
		t.Errorf("published %+v", out)
	}
	if p.Stats.Unattributed != 2 {
		t.Errorf("unattributed = %d, want 2", p.Stats.Unattributed)
	}
}

// nodeStatus is a single-frame NodeStatus from node 10 with the given uptime,
// tail byte computed for transfer id tid.
func nodeStatus(at time.Time, uptime uint32, tid int) string {
	return fmt.Sprintf("(%d.%06d) can0 1001550A#%02X%02X%02X%02X00EFBE%02X",
		at.Unix(), at.Nanosecond()/1000, byte(uptime), byte(uptime>>8), byte(uptime>>16), byte(uptime>>24), 0xC0|tid&0x1F)
}

func TestWindowsAreCutOnCaptureTime(t *testing.T) {
	t0 := time.Date(2026, 11, 25, 10, 0, 0, 0, time.UTC)
	var lines []string
	for i := 0; i < 25; i++ { // 2.5 s of NodeStatus at 10 Hz
		lines = append(lines, nodeStatus(t0.Add(time.Duration(i)*100*time.Millisecond), uint32(i), i))
	}
	p := New(Config{TenantID: tenant, ObjectType: "Subsystem", Interval: time.Second})
	out := run(t, p, lines)
	if len(out) != 3 {
		t.Fatalf("got %d messages, want 3 windows", len(out))
	}
	for i, want := range []struct {
		ts     string
		uptime float64
	}{{"2026-11-25T10:00:00.900Z", 9}, {"2026-11-25T10:00:01.900Z", 19}, {"2026-11-25T10:00:02.400Z", 24}} {
		m := out[i].Message
		if m.TS != want.ts || m.Metrics["node_uptime_s"] != want.uptime || m.Seq != uint64(i+1) {
			t.Errorf("window %d = ts %s uptime %v seq %d, want %s %v", i, m.TS, m.Metrics["node_uptime_s"], m.Seq, want.ts, want.uptime)
		}
	}
}

func TestAGapInTheCaptureSkipsEmptyWindows(t *testing.T) {
	t0 := time.Date(2026, 11, 25, 10, 0, 0, 0, time.UTC)
	lines := []string{nodeStatus(t0, 1, 1), nodeStatus(t0.Add(5500*time.Millisecond), 6, 2)}
	out := run(t, New(Config{TenantID: tenant, ObjectType: "Subsystem", Interval: time.Second}), lines)
	if len(out) != 2 {
		t.Fatalf("got %d messages, want 2: empty windows publish nothing", len(out))
	}
}

func TestOtherInterfacesAndBadLinesAreCounted(t *testing.T) {
	p := New(Config{TenantID: tenant, ObjectType: "Subsystem", Interval: time.Second, Iface: "can1"})
	run(t, p, []string{
		"(1795600000.001000) can0 1001550A#D204000055EFBEC3",
		"not a candump line",
		"(1795600000.002000) can1 123#R",
		"(1795600000.003000) can1 1001550A##1D204000055EFBEC3",
	})
	if p.Stats.OtherIface != 1 || p.Stats.BadLines != 1 || p.Stats.Unsupported != 2 {
		t.Errorf("stats = %+v", p.Stats)
	}
}
