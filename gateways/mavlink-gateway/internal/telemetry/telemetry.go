// Package telemetry turns MAVLink frames into messages that follow the S2
// telemetry contract v1 (S2: services/ingestion-connectors/src/s2_ingest/contracts.py).
//
//	MQTT topic    s2/v1/{tenant_id}/{ObjectType}/{object_pk}/telemetry
//	MQTT payload  {"ts": RFC 3339, "metrics": {name: number|string|bool},
//	               "position": {lat, lon, alt_m, heading_deg, speed_mps}, "seq": n}
//
// MAVLink arrives at tens of messages per second per vehicle. The aggregator
// keeps the latest value of each metric and emits one message per vehicle per
// flush, so the publish rate is set by the caller, not by the autopilot.
package telemetry

import (
	"fmt"
	"math"
	"sort"
	"sync"
	"time"

	"github.com/veerendra-saraswathi/S1.a/gateways/mavlink-gateway/internal/mavlink"
)

// Position is the contract's optional position block.
type Position struct {
	Lat        float64  `json:"lat"`
	Lon        float64  `json:"lon"`
	AltM       *float64 `json:"alt_m,omitempty"`
	HeadingDeg *float64 `json:"heading_deg,omitempty"`
	SpeedMps   *float64 `json:"speed_mps,omitempty"`
}

// Message is the MQTT payload.
type Message struct {
	TS       string         `json:"ts"`
	Metrics  map[string]any `json:"metrics,omitempty"`
	Position *Position      `json:"position,omitempty"`
	Seq      uint64         `json:"seq"`
}

// Outbound is one message ready to publish.
type Outbound struct {
	Topic   string
	Message Message
}

// Config says how MAVLink systems map onto S2 objects.
type Config struct {
	TenantID   string           // UUID of the S2 tenant
	ObjectType string           // S2 object type, e.g. CommercialDrone
	Vehicles   map[uint8]string // MAVLink system id -> object primary key
}

type vehicle struct {
	metrics  map[string]any
	position *Position
	seq      uint64
}

// Aggregator is safe for concurrent use.
type Aggregator struct {
	cfg      Config
	mu       sync.Mutex
	vehicles map[uint8]*vehicle
}

func NewAggregator(cfg Config) *Aggregator {
	return &Aggregator{cfg: cfg, vehicles: make(map[uint8]*vehicle)}
}

// ObjectKey is the S2 primary key for a MAVLink system id. Systems that are not
// in the configured map get "MAV-<sysid>"; S2 stores readings for objects that
// do not exist yet, so an unmapped vehicle is visible rather than lost.
func (a *Aggregator) ObjectKey(sysID uint8) string {
	if key, ok := a.cfg.Vehicles[sysID]; ok {
		return key
	}
	return fmt.Sprintf("MAV-%d", sysID)
}

// Apply records what one frame says about its vehicle.
func (a *Aggregator) Apply(f mavlink.Frame) {
	a.mu.Lock()
	defer a.mu.Unlock()

	if f.MsgID == mavlink.MsgHeartbeat {
		hb := mavlink.DecodeHeartbeat(f.Payload)
		if hb.Autopilot == mavlink.AutopilotInvalid {
			return // ground station, gimbal or companion computer, not a vehicle
		}
		v := a.vehicle(f.SysID)
		v.metrics["armed"] = hb.Armed()
		v.metrics["custom_mode"] = float64(hb.CustomMode)
		v.metrics["system_state"] = mavlink.SystemState(hb.SystemStatus)
		return
	}

	v := a.vehicle(f.SysID)
	switch f.MsgID {
	case mavlink.MsgSysStatus:
		s := mavlink.DecodeSysStatus(f.Payload)
		v.set("cpu_load_pct", s.LoadPct)
		v.setPtr("battery_voltage", s.VoltageV)
		v.setPtr("battery_current_a", s.CurrentA)
		v.setPtr("battery_remaining_pct", s.BatteryRemainingPct)
	case mavlink.MsgBatteryStatus:
		b := mavlink.DecodeBatteryStatus(f.Payload)
		if b.ID == 0 {
			v.setPtr("battery_temperature_c", b.TemperatureC)
		}
	case mavlink.MsgGPSRawInt:
		g := mavlink.DecodeGPSRawInt(f.Payload)
		v.set("gps_fix_type", float64(g.FixType))
		v.setPtr("gps_satellites", g.Satellites)
		v.setPtr("gps_hdop", g.HDOP)
	case mavlink.MsgAttitude:
		at := mavlink.DecodeAttitude(f.Payload)
		v.set("roll_deg", at.RollDeg)
		v.set("pitch_deg", at.PitchDeg)
		v.set("yaw_deg", at.YawDeg)
	case mavlink.MsgVFRHUD:
		h := mavlink.DecodeVFRHUD(f.Payload)
		v.set("airspeed_mps", h.AirspeedMps)
		v.set("throttle_pct", h.ThrottlePct)
	case mavlink.MsgGlobalPositionInt:
		g := mavlink.DecodeGlobalPositionInt(f.Payload)
		v.set("rel_alt_m", g.RelAltM)
		v.set("climb_rate_mps", g.ClimbMps)
		// An autopilot without a fix reports 0,0. Do not plot that in the Gulf of Guinea.
		if (g.LatDeg != 0 || g.LonDeg != 0) && math.Abs(g.LatDeg) <= 90 && math.Abs(g.LonDeg) <= 180 {
			alt, speed := g.AltMSLm, g.GroundSpeedMps
			v.position = &Position{Lat: g.LatDeg, Lon: g.LonDeg, AltM: &alt, HeadingDeg: g.HeadingDeg, SpeedMps: &speed}
		}
	}
}

// Flush returns one message for every vehicle that reported something since the
// previous flush, ordered by system id, and clears what it returned. The
// timestamp is the gateway's clock: MAVLink's time_boot_ms is not wall time.
func (a *Aggregator) Flush(now time.Time) []Outbound {
	a.mu.Lock()
	defer a.mu.Unlock()

	ids := make([]int, 0, len(a.vehicles))
	for id := range a.vehicles {
		ids = append(ids, int(id))
	}
	sort.Ints(ids)

	ts := now.UTC().Format("2006-01-02T15:04:05.000Z")
	var out []Outbound
	for _, id := range ids {
		v := a.vehicles[uint8(id)]
		if len(v.metrics) == 0 && v.position == nil {
			continue
		}
		v.seq++
		out = append(out, Outbound{
			Topic:   fmt.Sprintf("s2/v1/%s/%s/%s/telemetry", a.cfg.TenantID, a.cfg.ObjectType, a.ObjectKey(uint8(id))),
			Message: Message{TS: ts, Metrics: v.metrics, Position: v.position, Seq: v.seq},
		})
		v.metrics, v.position = make(map[string]any), nil
	}
	return out
}

func (a *Aggregator) vehicle(sysID uint8) *vehicle {
	v, ok := a.vehicles[sysID]
	if !ok {
		v = &vehicle{metrics: make(map[string]any)}
		a.vehicles[sysID] = v
	}
	return v
}

// set stores a number, ignoring NaN and infinities: JSON cannot carry them and
// a sensor that reports them has nothing useful to say.
func (v *vehicle) set(name string, value float64) {
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return
	}
	v.metrics[name] = value
}

func (v *vehicle) setPtr(name string, value *float64) {
	if value != nil {
		v.set(name, *value)
	}
}
