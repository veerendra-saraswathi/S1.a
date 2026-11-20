package telemetry

import (
	"bufio"
	"encoding/hex"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/veerendra-saraswathi/S1.a/gateways/mavlink-gateway/internal/mavlink"
)

// stream is one vehicle-second's worth of what an autopilot sends, as raw bytes:
// the golden frames for every decoded message, in a 10 Hz mix.
func stream(b *testing.B) ([]byte, int) {
	b.Helper()
	file, err := os.Open("../mavlink/testdata/golden.txt")
	if err != nil {
		b.Fatal(err)
	}
	defer file.Close()
	frames := make(map[string][]byte)
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		if name, encoded, ok := strings.Cut(scanner.Text(), " "); ok && !strings.HasPrefix(name, "#") {
			frames[name], _ = hex.DecodeString(encoded)
		}
	}
	var out []byte
	n := 0
	for i := 0; i < 10; i++ {
		for _, name := range []string{"global_position_int_v2", "attitude_v2", "vfr_hud_v2"} {
			out = append(out, frames[name]...)
			n++
		}
	}
	for _, name := range []string{"heartbeat_v2", "sys_status_v2", "gps_raw_int_v2", "system_time_v2"} {
		out = append(out, frames[name]...)
		n++
	}
	return out, n
}

// BenchmarkParseAndApply is the gateway's receive path: bytes to frames to the
// aggregator, per frame.
func BenchmarkParseAndApply(b *testing.B) {
	raw, n := stream(b)
	a := NewAggregator(Config{TenantID: tenant, ObjectType: "CommercialDrone"})
	now := time.Now()
	var p mavlink.Parser
	b.SetBytes(int64(len(raw)))
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		p.Reset()
		for _, f := range p.Write(raw) {
			a.Apply(f, now)
		}
		if i%10 == 9 {
			a.Flush(now)
		}
	}
	b.ReportMetric(float64(b.Elapsed().Nanoseconds())/float64(b.N*n), "ns/frame")
}
