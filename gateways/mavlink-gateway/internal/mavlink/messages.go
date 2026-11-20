package mavlink

import (
	"encoding/binary"
	"math"
)

// Message ids from the MAVLink common dialect.
const (
	MsgHeartbeat         uint32 = 0
	MsgSysStatus         uint32 = 1
	MsgSystemTime        uint32 = 2
	MsgGPSRawInt         uint32 = 24
	MsgAttitude          uint32 = 30
	MsgGlobalPositionInt uint32 = 33
	MsgVFRHUD            uint32 = 74
	MsgBatteryStatus     uint32 = 147
)

type definition struct {
	crcExtra byte // per-message checksum seed, from the dialect XML
	length   int  // payload length without extension fields
}

// definitions lists every message the parser will verify and return.
// To support another message: add its id, CRC_EXTRA and base length here, then a
// decoder below. Field offsets follow MAVLink wire order (fields sorted by type
// size, largest first), not the order in the XML.
var definitions = map[uint32]definition{
	MsgHeartbeat:         {50, 9},
	MsgSysStatus:         {124, 31},
	MsgSystemTime:        {137, 12},
	MsgGPSRawInt:         {24, 30},
	MsgAttitude:          {39, 28},
	MsgGlobalPositionInt: {104, 28},
	MsgVFRHUD:            {20, 20},
	MsgBatteryStatus:     {154, 36},
}

// AutopilotInvalid marks a component that is not a flight controller
// (ground station, gimbal, companion computer).
const AutopilotInvalid uint8 = 8

const modeFlagSafetyArmed uint8 = 0x80

var le = binary.LittleEndian

func f32(b []byte) float64 { return float64(math.Float32frombits(le.Uint32(b))) }

// Heartbeat is HEARTBEAT (#0).
type Heartbeat struct {
	CustomMode   uint32
	Type         uint8
	Autopilot    uint8
	BaseMode     uint8
	SystemStatus uint8
}

func (h Heartbeat) Armed() bool { return h.BaseMode&modeFlagSafetyArmed != 0 }

func DecodeHeartbeat(p []byte) Heartbeat {
	return Heartbeat{CustomMode: le.Uint32(p[0:]), Type: p[4], Autopilot: p[5], BaseMode: p[6], SystemStatus: p[7]}
}

var systemStates = [...]string{
	"UNINIT", "BOOT", "CALIBRATING", "STANDBY", "ACTIVE",
	"CRITICAL", "EMERGENCY", "POWEROFF", "FLIGHT_TERMINATION",
}

// SystemState names a MAV_STATE value.
func SystemState(v uint8) string {
	if int(v) < len(systemStates) {
		return systemStates[v]
	}
	return "UNKNOWN"
}

// SysStatus is the part of SYS_STATUS (#1) the gateway uses.
// A nil field means the autopilot reported "not measured".
type SysStatus struct {
	LoadPct             float64
	VoltageV            *float64
	CurrentA            *float64
	BatteryRemainingPct *float64
}

func DecodeSysStatus(p []byte) SysStatus {
	s := SysStatus{LoadPct: float64(le.Uint16(p[12:])) / 10}
	if v := le.Uint16(p[14:]); v != math.MaxUint16 {
		s.VoltageV = ptr(float64(v) / 1000)
	}
	if c := int16(le.Uint16(p[16:])); c != -1 {
		s.CurrentA = ptr(float64(c) / 100)
	}
	if r := int8(p[30]); r != -1 {
		s.BatteryRemainingPct = ptr(float64(r))
	}
	return s
}

// SystemTime is SYSTEM_TIME (#2): the autopilot's UTC against its boot clock.
// UnixUsec is 0 when the autopilot has no time source (usually no GNSS fix yet).
type SystemTime struct {
	UnixUsec uint64
	BootMs   uint32
}

func DecodeSystemTime(p []byte) SystemTime {
	return SystemTime{UnixUsec: le.Uint64(p[0:]), BootMs: le.Uint32(p[8:])}
}

// GPSRawInt is the part of GPS_RAW_INT (#24) the gateway uses.
type GPSRawInt struct {
	FixType    uint8
	Satellites *float64
	HDOP       *float64
}

func DecodeGPSRawInt(p []byte) GPSRawInt {
	g := GPSRawInt{FixType: p[28]}
	if eph := le.Uint16(p[20:]); eph != math.MaxUint16 {
		g.HDOP = ptr(float64(eph) / 100)
	}
	if n := p[29]; n != math.MaxUint8 {
		g.Satellites = ptr(float64(n))
	}
	return g
}

// Attitude is ATTITUDE (#30), converted to degrees, sampled at BootMs on the
// autopilot's clock.
type Attitude struct {
	BootMs                    uint32
	RollDeg, PitchDeg, YawDeg float64
}

func DecodeAttitude(p []byte) Attitude {
	const deg = 180 / math.Pi
	return Attitude{BootMs: le.Uint32(p[0:]), RollDeg: f32(p[4:]) * deg, PitchDeg: f32(p[8:]) * deg, YawDeg: f32(p[12:]) * deg}
}

// GlobalPositionInt is GLOBAL_POSITION_INT (#33) in SI units.
type GlobalPositionInt struct {
	BootMs         uint32 // sample time on the autopilot's clock
	LatDeg, LonDeg float64
	AltMSLm        float64
	RelAltM        float64
	GroundSpeedMps float64
	ClimbMps       float64  // positive up
	HeadingDeg     *float64 // nil when unknown
}

func DecodeGlobalPositionInt(p []byte) GlobalPositionInt {
	vx := float64(int16(le.Uint16(p[20:]))) / 100
	vy := float64(int16(le.Uint16(p[22:]))) / 100
	vz := float64(int16(le.Uint16(p[24:]))) / 100 // NED: positive down
	g := GlobalPositionInt{
		BootMs:         le.Uint32(p[0:]),
		LatDeg:         float64(int32(le.Uint32(p[4:]))) / 1e7,
		LonDeg:         float64(int32(le.Uint32(p[8:]))) / 1e7,
		AltMSLm:        float64(int32(le.Uint32(p[12:]))) / 1000,
		RelAltM:        float64(int32(le.Uint32(p[16:]))) / 1000,
		GroundSpeedMps: math.Hypot(vx, vy),
		ClimbMps:       0 - vz, // not -vz: that would report -0 in level flight
	}
	if hdg := le.Uint16(p[26:]); hdg != math.MaxUint16 {
		g.HeadingDeg = ptr(math.Mod(float64(hdg)/100, 360))
	}
	return g
}

// VFRHUD is the part of VFR_HUD (#74) the gateway uses.
type VFRHUD struct {
	AirspeedMps    float64
	GroundSpeedMps float64
	ThrottlePct    float64
}

func DecodeVFRHUD(p []byte) VFRHUD {
	return VFRHUD{AirspeedMps: f32(p[0:]), GroundSpeedMps: f32(p[4:]), ThrottlePct: float64(le.Uint16(p[18:]))}
}

// BatteryStatus is the part of BATTERY_STATUS (#147) the gateway uses.
type BatteryStatus struct {
	ID           uint8
	TemperatureC *float64
}

func DecodeBatteryStatus(p []byte) BatteryStatus {
	b := BatteryStatus{ID: p[32]}
	if t := int16(le.Uint16(p[8:])); t != math.MaxInt16 {
		b.TemperatureC = ptr(float64(t) / 100)
	}
	return b
}

func ptr(v float64) *float64 { return &v }
