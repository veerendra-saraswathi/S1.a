// Package bus turns a DroneCAN capture into S2 telemetry: candump lines in,
// contract messages out.
//
// Each node maps to an S2 object (by default a Subsystem). Its NodeStatus and
// ESC status become that object's metrics. Actuators are filed by actuator id:
// an actuator's status and the commands sent to it land on the same object, so
// commanded and measured position sit side by side, which is what an actuator
// fault twin compares. Commands are broadcast by the flight controller, not by
// the actuator, so an actuator's object is the one named for it with
// -actuators, or else the object of the node that reports its status.
//
// Messages are cut on capture time, not on the gateway's clock: one message per
// object per interval of the capture, stamped with its newest frame's capture
// time. Replaying the same log gives the same messages. On a live capture the
// last window is published when the next frame arrives; every DroneCAN node
// sends NodeStatus at least once a second, so a working bus never stalls it.
package bus

import (
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/veerendra-saraswathi/S1.a/gateways/can-gateway/internal/candump"
	"github.com/veerendra-saraswathi/S1.a/gateways/can-gateway/internal/dronecan"
	"github.com/veerendra-saraswathi/S1.a/gateways/internal/s2"
)

// ClockSource names how ts was taken: the capturing host's kernel receive time.
const ClockSource = "capture"

// Config says how CAN nodes and actuators map onto S2 objects.
type Config struct {
	TenantID   string
	ObjectType string
	Nodes      map[uint8]string // node id -> object key; unmapped nodes are CAN-<id>
	Actuators  map[uint8]string // actuator id -> object key, overriding the reporting node's
	Interval   time.Duration    // one message per object per this much capture time
	Iface      string           // only frames from this interface; "" takes all
}

// Stats counts lines and what became of them, beyond the reassembler's own.
type Stats struct {
	Lines        uint64
	BadLines     uint64 // not candump log lines
	Unsupported  uint64 // remote and CAN FD frames
	OtherIface   uint64
	BadPayload   uint64 // checked transfers whose fields did not fit
	Unattributed uint64 // commands for an actuator no node has reported yet
	Published    uint64
}

type object struct {
	metrics map[string]any
	newest  time.Time
	seq     uint64
}

// Pipeline is not safe for concurrent use.
type Pipeline struct {
	cfg     Config
	reasm   dronecan.Reassembler
	objects map[string]*object
	owner   map[uint8]string // actuator id -> object key, learned from status
	next    time.Time        // capture time at which the current window closes
	Stats   Stats
}

func New(cfg Config) *Pipeline {
	return &Pipeline{
		cfg:     cfg,
		reasm:   dronecan.Reassembler{Known: dronecan.Known},
		objects: make(map[string]*object),
		owner:   make(map[uint8]string),
	}
}

// Transport returns the reassembler's counters.
func (p *Pipeline) Transport() dronecan.Stats { return p.reasm.Stats }

// Line reads one candump line, calling emit for every message a closed window produces.
func (p *Pipeline) Line(line string, emit func(s2.Outbound)) {
	p.Stats.Lines++
	frame, iface, err := candump.Parse(line)
	switch {
	case err == candump.ErrUnsupported:
		p.Stats.Unsupported++
		return
	case err != nil:
		p.Stats.BadLines++
		return
	case p.cfg.Iface != "" && iface != p.cfg.Iface:
		p.Stats.OtherIface++
		return
	}
	tr, ok := p.reasm.Push(frame)
	if !ok {
		return
	}
	if p.next.IsZero() {
		p.next = tr.Time.Add(p.cfg.Interval)
	} else if !tr.Time.Before(p.next) {
		p.Flush(emit)
		for !tr.Time.Before(p.next) {
			p.next = p.next.Add(p.cfg.Interval)
		}
	}
	p.apply(tr)
}

// Flush emits one message for every object that changed since the last flush,
// in key order.
func (p *Pipeline) Flush(emit func(s2.Outbound)) {
	keys := make([]string, 0, len(p.objects))
	for k, o := range p.objects {
		if len(o.metrics) > 0 {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	for _, k := range keys {
		o := p.objects[k]
		o.seq++
		o.metrics["clock_source"] = ClockSource
		emit(s2.Outbound{
			Topic:   s2.Topic(p.cfg.TenantID, p.cfg.ObjectType, k),
			Message: s2.Message{TS: s2.Timestamp(o.newest), Metrics: o.metrics, Seq: o.seq},
		})
		p.Stats.Published++
		o.metrics = make(map[string]any)
	}
}

func (p *Pipeline) nodeKey(node uint8) string {
	if k, ok := p.cfg.Nodes[node]; ok {
		return k
	}
	return fmt.Sprintf("CAN-%d", node)
}

func (p *Pipeline) actuatorKey(id uint8) (string, bool) {
	if k, ok := p.cfg.Actuators[id]; ok {
		return k, true
	}
	k, ok := p.owner[id]
	return k, ok
}

func (p *Pipeline) object(key string, at time.Time) *object {
	o, ok := p.objects[key]
	if !ok {
		o = &object{metrics: make(map[string]any)}
		p.objects[key] = o
	}
	if at.After(o.newest) {
		o.newest = at
	}
	return o
}

func (p *Pipeline) apply(tr dronecan.Transfer) {
	switch tr.DataTypeID {
	case dronecan.NodeStatusID:
		s, err := dronecan.DecodeNodeStatus(tr.Payload)
		if err != nil {
			p.Stats.BadPayload++
			return
		}
		o := p.object(p.nodeKey(tr.SourceNode), tr.Time)
		o.metrics["node_uptime_s"] = float64(s.UptimeSec)
		o.metrics["node_health"] = dronecan.HealthName(s.Health)
		o.metrics["node_mode"] = dronecan.ModeName(s.Mode)
		o.metrics["node_vendor_status"] = float64(s.VendorSpecific)

	case dronecan.ESCStatusID:
		e, err := dronecan.DecodeESCStatus(tr.Payload)
		if err != nil {
			p.Stats.BadPayload++
			return
		}
		o := p.object(p.nodeKey(tr.SourceNode), tr.Time)
		prefix := fmt.Sprintf("esc_%d_", e.Index)
		set(o, prefix+"rpm", float64(e.RPM))
		set(o, prefix+"voltage", e.VoltageV)
		set(o, prefix+"current_a", e.CurrentA)
		set(o, prefix+"temperature_c", math.Round((e.TemperatureK-273.15)*100)/100) // float16 holds 0.25 K
		set(o, prefix+"power_pct", float64(e.PowerRatingPct))
		set(o, prefix+"error_count", float64(e.ErrorCount))

	case dronecan.ActuatorStatusID:
		a, err := dronecan.DecodeActuatorStatus(tr.Payload)
		if err != nil {
			p.Stats.BadPayload++
			return
		}
		key, ok := p.actuatorKey(a.ID)
		if !ok {
			key = p.nodeKey(tr.SourceNode)
			p.owner[a.ID] = key
		}
		o := p.object(key, tr.Time)
		prefix := fmt.Sprintf("actuator_%d_", a.ID)
		set(o, prefix+"position", a.Position)
		set(o, prefix+"force", a.Force) // NaN, unmeasured, is left out
		set(o, prefix+"speed", a.Speed)
		set(o, prefix+"power_pct", float64(a.PowerRatingPct))

	case dronecan.ActuatorArrayCommandID:
		cmds, err := dronecan.DecodeActuatorArrayCommand(tr.Payload)
		if err != nil {
			p.Stats.BadPayload++
			return
		}
		for _, c := range cmds {
			key, ok := p.actuatorKey(c.ID)
			if !ok {
				p.Stats.Unattributed++
				continue
			}
			o := p.object(key, tr.Time)
			prefix := fmt.Sprintf("actuator_%d_", c.ID)
			set(o, prefix+"command", c.Value)
			o.metrics[prefix+"command_type"] = dronecan.CommandTypeName(c.Type)
		}
	}
}

// set stores a number, leaving out NaN and infinities: JSON cannot carry them,
// and DroneCAN uses NaN for "not measured".
func set(o *object, name string, v float64) {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return
	}
	o.metrics[name] = v
}
