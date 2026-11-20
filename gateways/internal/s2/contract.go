// Package s2 is the S2 telemetry contract v1, as every S1.a gateway publishes it
// (S2: services/ingestion-connectors/src/s2_ingest/contracts.py).
//
//	MQTT topic    s2/v1/{tenant_id}/{ObjectType}/{object_pk}/telemetry
//	MQTT payload  {"ts": RFC 3339, "metrics": {name: number|string|bool},
//	               "position": {lat, lon, alt_m, heading_deg, speed_mps}, "seq": n}
//
// S2 rejects fields it does not know, so anything a gateway adds, such as the
// clock source, travels as a metric.
package s2

import (
	"fmt"
	"regexp"
	"time"
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

// TimeLayout is RFC 3339 in UTC with milliseconds.
const TimeLayout = "2006-01-02T15:04:05.000Z"

// Timestamp formats t for the contract's ts.
func Timestamp(t time.Time) string { return t.UTC().Format(TimeLayout) }

// Topic is where an object's telemetry is published.
func Topic(tenantID, objectType, key string) string {
	return fmt.Sprintf("s2/v1/%s/%s/%s/telemetry", tenantID, objectType, key)
}

var metricName = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)

// ValidMetric reports whether S2 accepts name as a metric name.
func ValidMetric(name string) bool { return metricName.MatchString(name) }
