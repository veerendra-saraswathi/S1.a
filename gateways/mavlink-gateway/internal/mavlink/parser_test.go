package mavlink

import (
	"bufio"
	"encoding/hex"
	"math"
	"os"
	"strings"
	"testing"
)

// golden loads frames encoded by pymavlink (see tools/mavlink-sim/gen_golden.py).
func golden(t *testing.T) map[string][]byte {
	t.Helper()
	file, err := os.Open("testdata/golden.txt")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	out := make(map[string][]byte)
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "#") || strings.TrimSpace(line) == "" {
			continue
		}
		name, encoded, _ := strings.Cut(line, " ")
		raw, err := hex.DecodeString(encoded)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		out[name] = raw
	}
	return out
}

func one(t *testing.T, raw []byte) Frame {
	t.Helper()
	var p Parser
	frames := p.Write(raw)
	if len(frames) != 1 {
		t.Fatalf("got %d frames, want 1 (stats %+v)", len(frames), p.Stats)
	}
	return frames[0]
}

func near(t *testing.T, name string, got, want float64) {
	t.Helper()
	if math.Abs(got-want) > 1e-4 {
		t.Errorf("%s = %v, want %v", name, got, want)
	}
}

func TestDecodesPymavlinkFrames(t *testing.T) {
	g := golden(t)
	for _, tag := range []string{"v1", "v2"} {
		t.Run(tag, func(t *testing.T) {
			f := one(t, g["heartbeat_"+tag])
			if f.SysID != 7 || f.CompID != 1 || f.MsgID != MsgHeartbeat {
				t.Fatalf("header = %+v", f)
			}
			hb := DecodeHeartbeat(f.Payload)
			if !hb.Armed() || hb.CustomMode != 3 || hb.Autopilot != 3 || SystemState(hb.SystemStatus) != "ACTIVE" {
				t.Errorf("heartbeat = %+v", hb)
			}
			if gcs := DecodeHeartbeat(one(t, g["gcs_heartbeat_"+tag]).Payload); gcs.Autopilot != AutopilotInvalid || gcs.Armed() {
				t.Errorf("gcs heartbeat = %+v", gcs)
			}

			s := DecodeSysStatus(one(t, g["sys_status_"+tag]).Payload)
			near(t, "load", s.LoadPct, 23.5)
			near(t, "voltage", *s.VoltageV, 24.31)
			near(t, "current", *s.CurrentA, 12.5)
			near(t, "remaining", *s.BatteryRemainingPct, 76)
			if u := DecodeSysStatus(one(t, g["sys_status_unknown_"+tag]).Payload); u.VoltageV != nil || u.CurrentA != nil || u.BatteryRemainingPct != nil {
				t.Errorf("unknown battery values should be nil: %+v", u)
			}

			gps := DecodeGPSRawInt(one(t, g["gps_raw_int_"+tag]).Payload)
			if gps.FixType != 3 {
				t.Errorf("fix type = %d", gps.FixType)
			}
			near(t, "satellites", *gps.Satellites, 14)
			near(t, "hdop", *gps.HDOP, 0.8)

			at := DecodeAttitude(one(t, g["attitude_"+tag]).Payload)
			near(t, "roll", at.RollDeg, 10)
			near(t, "pitch", at.PitchDeg, -5)
			near(t, "yaw", at.YawDeg, 90)

			pos := DecodeGlobalPositionInt(one(t, g["global_position_int_"+tag]).Payload)
			near(t, "lat", pos.LatDeg, 17.4401)
			near(t, "lon", pos.LonDeg, 78.3489)
			near(t, "alt", pos.AltMSLm, 580.5)
			near(t, "rel alt", pos.RelAltM, 40.25)
			near(t, "ground speed", pos.GroundSpeedMps, 5) // 3 m/s north, 4 m/s east
			near(t, "climb", pos.ClimbMps, 1.5)
			near(t, "heading", *pos.HeadingDeg, 87)
			if nh := DecodeGlobalPositionInt(one(t, g["global_position_no_heading_"+tag]).Payload); nh.HeadingDeg != nil {
				t.Errorf("heading 65535 should decode as unknown, got %v", *nh.HeadingDeg)
			}

			hud := DecodeVFRHUD(one(t, g["vfr_hud_"+tag]).Payload)
			near(t, "airspeed", hud.AirspeedMps, 13.5)
			near(t, "hud ground speed", hud.GroundSpeedMps, 12)
			near(t, "throttle", hud.ThrottlePct, 55)

			bat := DecodeBatteryStatus(one(t, g["battery_status_"+tag]).Payload)
			near(t, "battery temperature", *bat.TemperatureC, 31.5)
			if bat.ID != 0 {
				t.Errorf("battery id = %d", bat.ID)
			}
		})
	}
}

func TestStreamSplitAcrossWritesWithNoise(t *testing.T) {
	g := golden(t)
	var stream []byte
	stream = append(stream, 0x00, 0x11, 0x22) // line noise before the first frame
	stream = append(stream, g["heartbeat_v2"]...)
	stream = append(stream, g["undecoded_v2"]...)
	stream = append(stream, g["attitude_v1"]...)
	stream = append(stream, g["global_position_int_v2"]...)

	var p Parser
	var ids []uint32
	for _, b := range stream { // worst case: one byte at a time
		for _, f := range p.Write([]byte{b}) {
			ids = append(ids, f.MsgID)
		}
	}
	want := []uint32{MsgHeartbeat, MsgAttitude, MsgGlobalPositionInt}
	if len(ids) != len(want) {
		t.Fatalf("ids = %v, want %v (stats %+v)", ids, want, p.Stats)
	}
	for i := range want {
		if ids[i] != want[i] {
			t.Fatalf("ids = %v, want %v", ids, want)
		}
	}
	if p.Stats.Garbage != 3 || p.Stats.Unknown != 1 || p.Stats.BadCRC != 0 {
		t.Errorf("stats = %+v", p.Stats)
	}
}

func TestCorruptFrameIsRejectedAndParserRecovers(t *testing.T) {
	g := golden(t)
	bad := append([]byte(nil), g["sys_status_v2"]...)
	bad[14] ^= 0xFF // flip a payload byte
	var p Parser
	frames := p.Write(append(bad, g["heartbeat_v2"]...))
	if len(frames) != 1 || frames[0].MsgID != MsgHeartbeat {
		t.Fatalf("frames = %+v", frames)
	}
	if p.Stats.BadCRC == 0 {
		t.Errorf("corruption was not counted: %+v", p.Stats)
	}
}

func TestSignedAndIncompatibleV2Frames(t *testing.T) {
	g := golden(t)
	// A signed frame keeps the same checksum and carries 13 extra bytes after it.
	signed := append([]byte(nil), g["heartbeat_v2"]...)
	signed[2] |= incompatSigned
	signed = fixCRC(signed, definitions[MsgHeartbeat].crcExtra)
	signed = append(signed, make([]byte, signatureLen)...)
	var p Parser
	if frames := p.Write(append(signed, g["attitude_v2"]...)); len(frames) != 2 {
		t.Fatalf("signed frame: got %d frames, want 2 (stats %+v)", len(frames), p.Stats)
	}

	unknown := append([]byte(nil), g["heartbeat_v2"]...)
	unknown[2] |= 0x02
	unknown = fixCRC(unknown, definitions[MsgHeartbeat].crcExtra)
	p = Parser{}
	if frames := p.Write(unknown); len(frames) != 0 || p.Stats.Dropped != 1 {
		t.Fatalf("unknown incompat flag: frames=%d stats=%+v", len(frames), p.Stats)
	}
}

// fixCRC recomputes the checksum of an unsigned v2 frame after its header was edited.
func fixCRC(frame []byte, extra byte) []byte {
	end := len(frame) - crcLen
	crc := crcInit
	for _, c := range frame[1:end] {
		crc = crcAccumulate(c, crc)
	}
	crc = crcAccumulate(extra, crc)
	frame[end], frame[end+1] = byte(crc), byte(crc>>8)
	return frame
}
