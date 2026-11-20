package dronecan_test

import (
	"bufio"
	"math"
	"os"
	"strings"
	"testing"

	"github.com/veerendra-saraswathi/S1.a/gateways/can-gateway/internal/candump"
	"github.com/veerendra-saraswathi/S1.a/gateways/can-gateway/internal/dronecan"
)

// golden reads frames encoded by the dronecan package (tools/can-sim/gen_golden.py),
// grouped by case name, in order.
func golden(t *testing.T) map[string][]dronecan.Frame {
	t.Helper()
	file, err := os.Open("testdata/golden.log")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	out := make(map[string][]dronecan.Frame)
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		name, line, ok := strings.Cut(scanner.Text(), " ")
		if !ok || strings.HasPrefix(name, "#") {
			continue
		}
		f, _, err := candump.Parse(line)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		out[name] = append(out[name], f)
	}
	return out
}

func transfer(t *testing.T, frames []dronecan.Frame) dronecan.Transfer {
	t.Helper()
	r := dronecan.Reassembler{Known: dronecan.Known}
	var got []dronecan.Transfer
	for _, f := range frames {
		if tr, ok := r.Push(f); ok {
			got = append(got, tr)
		}
	}
	if len(got) != 1 {
		t.Fatalf("got %d transfers from %d frames, want 1 (stats %+v)", len(got), len(frames), r.Stats)
	}
	return got[0]
}

func near(t *testing.T, name string, got, want float64) {
	t.Helper()
	if math.Abs(got-want) > 1e-3*math.Max(1, math.Abs(want)) {
		t.Errorf("%s = %v, want %v", name, got, want)
	}
}

func TestDecodesDronecanFrames(t *testing.T) {
	g := golden(t)

	ns := transfer(t, g["node_status"])
	if ns.DataTypeID != dronecan.NodeStatusID || ns.SourceNode != 10 || ns.Priority != 16 || ns.TransferID != 3 {
		t.Errorf("node status header = %+v", ns)
	}
	s, err := dronecan.DecodeNodeStatus(ns.Payload)
	if err != nil || s.UptimeSec != 1234 || s.Health != 1 || s.Mode != 2 || s.SubMode != 5 || s.VendorSpecific != 0xBEEF {
		t.Errorf("node status = %+v, %v", s, err)
	}
	if dronecan.HealthName(s.Health) != "WARNING" || dronecan.ModeName(s.Mode) != "MAINTENANCE" {
		t.Errorf("names = %s %s", dronecan.HealthName(s.Health), dronecan.ModeName(s.Mode))
	}

	as := transfer(t, g["actuator_status"]) // two frames, CRC checked
	a, err := dronecan.DecodeActuatorStatus(as.Payload)
	if err != nil || a.ID != 3 || a.PowerRatingPct != 40 {
		t.Errorf("actuator status = %+v, %v", a, err)
	}
	near(t, "position", a.Position, 0.25)
	near(t, "force", a.Force, -1.5)
	near(t, "speed", a.Speed, 2)
	if want := "2026-11-25T09:46:40.003Z"; as.Time.Format("2006-01-02T15:04:05.000Z") != want {
		t.Errorf("transfer time = %s, want the last frame's, %s", as.Time, want)
	}

	u, err := dronecan.DecodeActuatorStatus(transfer(t, g["actuator_status_unmeasured"]).Payload)
	if err != nil || !math.IsNaN(u.Force) || !math.IsNaN(u.Speed) {
		t.Errorf("unmeasured force and speed should be NaN: %+v, %v", u, err)
	}
	near(t, "unmeasured position", u.Position, -0.5)

	e, err := dronecan.DecodeESCStatus(transfer(t, g["esc_status"]).Payload) // three frames
	if err != nil || e.ErrorCount != 7 || e.RPM != -12345 || e.PowerRatingPct != 55 || e.Index != 2 {
		t.Errorf("esc status = %+v, %v", e, err)
	}
	near(t, "voltage", e.VoltageV, 24.5)
	near(t, "current", e.CurrentA, 12.25)
	near(t, "temperature", e.TemperatureK, 313.15) // float16 holds it to 0.25 K

	cmds, err := dronecan.DecodeActuatorArrayCommand(transfer(t, g["array_command"]).Payload)
	if err != nil || len(cmds) != 2 || cmds[0].ID != 3 || cmds[0].Type != 1 || cmds[1].ID != 4 {
		t.Fatalf("array command = %+v, %v", cmds, err)
	}
	near(t, "command 3", cmds[0].Value, 0.5)
	near(t, "command 4", cmds[1].Value, -0.25)
}

func TestFramesThatAreNotOurMessagesAreCounted(t *testing.T) {
	g := golden(t)
	r := dronecan.Reassembler{Known: dronecan.Known}
	for _, name := range []string{"undecoded", "service_request", "standard_id"} {
		for _, f := range g[name] {
			if tr, ok := r.Push(f); ok {
				t.Errorf("%s produced a transfer: %+v", name, tr)
			}
		}
	}
	if r.Stats.Undecoded != 1 || r.Stats.Ignored != 2 {
		t.Errorf("stats = %+v, want 1 undecoded, 2 ignored", r.Stats)
	}
}

func TestACorruptMultiFrameTransferFailsItsCRC(t *testing.T) {
	frames := golden(t)["esc_status"]
	bad := append([]byte(nil), frames[1].Data...)
	bad[2] ^= 0x01
	frames[1].Data = bad
	r := dronecan.Reassembler{Known: dronecan.Known}
	for _, f := range frames {
		if _, ok := r.Push(f); ok {
			t.Fatal("a corrupted transfer was accepted")
		}
	}
	if r.Stats.BadCRC != 1 {
		t.Errorf("stats = %+v", r.Stats)
	}
}

func TestALostFrameDropsTheTransfer(t *testing.T) {
	frames := golden(t)["esc_status"]
	r := dronecan.Reassembler{Known: dronecan.Known}
	r.Push(frames[0])
	if _, ok := r.Push(frames[2]); ok { // the middle frame went missing: toggle is wrong
		t.Fatal("a transfer with a missing frame was accepted")
	}
	if r.Stats.Malformed != 1 {
		t.Errorf("stats = %+v", r.Stats)
	}
	if tr := transfer(t, golden(t)["esc_status"]); tr.SourceNode != 20 { // and a clean resend still works
		t.Errorf("source = %d", tr.SourceNode)
	}
}

func TestInterleavedTransfersFromTwoNodesBothComplete(t *testing.T) {
	g := golden(t)
	a, e := g["actuator_status"], g["esc_status"]
	r := dronecan.Reassembler{Known: dronecan.Known}
	var done []uint16
	for _, f := range []dronecan.Frame{a[0], e[0], a[1], e[1], e[2]} {
		if tr, ok := r.Push(f); ok {
			done = append(done, tr.DataTypeID)
		}
	}
	if len(done) != 2 || done[0] != dronecan.ActuatorStatusID || done[1] != dronecan.ESCStatusID {
		t.Errorf("completed = %v (stats %+v)", done, r.Stats)
	}
}

func TestShortPayloadIsAnError(t *testing.T) {
	if _, err := dronecan.DecodeNodeStatus([]byte{1, 2, 3}); err == nil {
		t.Error("a 3-byte node status decoded without error")
	}
	if _, err := dronecan.DecodeActuatorArrayCommand([]byte{1, 2, 3}); err == nil {
		t.Error("a 3-byte array command decoded without error")
	}
}
