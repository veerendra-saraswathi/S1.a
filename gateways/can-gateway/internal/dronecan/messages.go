package dronecan

import (
	"errors"
	"math"
)

// Data type ids and signatures, from the DSDL definitions
// (checked against the dronecan Python package by the golden tests).
const (
	NodeStatusID           uint16 = 341  // uavcan.protocol.NodeStatus
	ActuatorArrayCommandID uint16 = 1010 // uavcan.equipment.actuator.ArrayCommand
	ActuatorStatusID       uint16 = 1011 // uavcan.equipment.actuator.Status
	ESCStatusID            uint16 = 1034 // uavcan.equipment.esc.Status
)

var signatures = map[uint16]uint64{
	NodeStatusID:           0x0F0868D0C1A7C6F1,
	ActuatorArrayCommandID: 0xD8A7486238EC3AF3,
	ActuatorStatusID:       0x5E9BBA44FAF1EA04,
	ESCStatusID:            0xA9AF28AEA2FBB254,
}

// Known reports the signature of a type this package decodes.
func Known(dataType uint16) (uint64, bool) {
	s, ok := signatures[dataType]
	return s, ok
}

// ErrShort means a payload ended before its fields did.
var ErrShort = errors.New("dronecan: payload too short")

// bits reads DSDL fields the way UAVCAN v0 lays them out: bits taken from the
// most significant end of each byte, in declaration order, and a field wider
// than a byte stored little-endian (libcanard's canardDecodeScalar).
type bits struct {
	data []byte
	off  int // bit offset
	err  error
}

func (b *bits) uint(n int) uint64 {
	if b.err != nil {
		return 0
	}
	if b.off+n > len(b.data)*8 {
		b.err = ErrShort
		return 0
	}
	var buf [8]byte
	for i := 0; i < n; i++ { // copy n bits, MSB first
		src := b.off + i
		if b.data[src/8]&(0x80>>(src%8)) != 0 {
			buf[i/8] |= 0x80 >> (i % 8)
		}
	}
	if n%8 != 0 { // the last, partial byte holds its bits at the top
		buf[n/8] >>= 8 - n%8
	}
	b.off += n
	var v uint64
	for i := (n+7)/8 - 1; i >= 0; i-- {
		v = v<<8 | uint64(buf[i])
	}
	return v
}

func (b *bits) int(n int) int64 {
	v := b.uint(n)
	if v&(1<<(n-1)) != 0 {
		return int64(v) - 1<<n
	}
	return int64(v)
}

func (b *bits) float16() float64 { return halfToFloat(uint16(b.uint(16))) }

func (b *bits) skip(n int) { b.uint(n) }

func (b *bits) remaining() int { return len(b.data)*8 - b.off }

// halfToFloat converts IEEE 754 binary16.
func halfToFloat(h uint16) float64 {
	sign := 1.0
	if h&0x8000 != 0 {
		sign = -1
	}
	exp := int(h >> 10 & 0x1F)
	frac := float64(h & 0x3FF)
	switch exp {
	case 0:
		return sign * frac / 1024 * math.Pow(2, -14)
	case 0x1F:
		if frac != 0 {
			return math.NaN()
		}
		return math.Inf(int(sign))
	}
	return sign * (1 + frac/1024) * math.Pow(2, float64(exp-15))
}

// NodeStatus is uavcan.protocol.NodeStatus, which every node broadcasts at
// least once a second.
type NodeStatus struct {
	UptimeSec      uint32
	Health         uint8 // 0 OK, 1 WARNING, 2 ERROR, 3 CRITICAL
	Mode           uint8
	SubMode        uint8
	VendorSpecific uint16
}

func DecodeNodeStatus(p []byte) (NodeStatus, error) {
	b := bits{data: p}
	s := NodeStatus{
		UptimeSec:      uint32(b.uint(32)),
		Health:         uint8(b.uint(2)),
		Mode:           uint8(b.uint(3)),
		SubMode:        uint8(b.uint(3)),
		VendorSpecific: uint16(b.uint(16)),
	}
	return s, b.err
}

var healthNames = [...]string{"OK", "WARNING", "ERROR", "CRITICAL"}

// HealthName names a NodeStatus health value.
func HealthName(h uint8) string { return healthNames[h&3] }

// ModeName names a NodeStatus mode value.
func ModeName(m uint8) string {
	switch m {
	case 0:
		return "OPERATIONAL"
	case 1:
		return "INITIALIZATION"
	case 2:
		return "MAINTENANCE"
	case 3:
		return "SOFTWARE_UPDATE"
	case 7:
		return "OFFLINE"
	}
	return "UNKNOWN"
}

// ESCStatus is uavcan.equipment.esc.Status.
type ESCStatus struct {
	ErrorCount     uint32
	VoltageV       float64
	CurrentA       float64
	TemperatureK   float64
	RPM            int32
	PowerRatingPct uint8
	Index          uint8
}

func DecodeESCStatus(p []byte) (ESCStatus, error) {
	b := bits{data: p}
	s := ESCStatus{
		ErrorCount:     uint32(b.uint(32)),
		VoltageV:       b.float16(),
		CurrentA:       b.float16(),
		TemperatureK:   b.float16(),
		RPM:            int32(b.int(18)),
		PowerRatingPct: uint8(b.uint(7)),
		Index:          uint8(b.uint(5)),
	}
	return s, b.err
}

// ActuatorStatus is uavcan.equipment.actuator.Status. Force and speed are NaN
// when the actuator does not measure them.
type ActuatorStatus struct {
	ID             uint8
	Position       float64 // metres or radians
	Force          float64 // newtons or newton-metres
	Speed          float64 // metres or radians per second
	PowerRatingPct uint8
}

func DecodeActuatorStatus(p []byte) (ActuatorStatus, error) {
	b := bits{data: p}
	s := ActuatorStatus{ID: uint8(b.uint(8)), Position: b.float16(), Force: b.float16(), Speed: b.float16()}
	b.skip(1) // void1
	s.PowerRatingPct = uint8(b.uint(7))
	return s, b.err
}

// ActuatorCommand is one element of uavcan.equipment.actuator.ArrayCommand.
type ActuatorCommand struct {
	ID    uint8
	Type  uint8 // 0 unitless, 1 position, 2 force, 3 speed
	Value float64
}

// CommandTypeName names an actuator command type.
func CommandTypeName(t uint8) string {
	switch t {
	case 0:
		return "UNITLESS"
	case 1:
		return "POSITION"
	case 2:
		return "FORCE"
	case 3:
		return "SPEED"
	}
	return "UNKNOWN"
}

// DecodeActuatorArrayCommand reads up to 15 commands. The array is the type's
// last field, so it carries no length prefix (tail array optimisation): its
// length is whatever the payload holds.
func DecodeActuatorArrayCommand(p []byte) ([]ActuatorCommand, error) {
	b := bits{data: p}
	var out []ActuatorCommand
	for b.remaining() >= 32 && len(out) < 15 {
		out = append(out, ActuatorCommand{ID: uint8(b.uint(8)), Type: uint8(b.uint(8)), Value: b.float16()})
	}
	if b.remaining() != 0 && b.err == nil {
		return out, ErrShort
	}
	return out, b.err
}
