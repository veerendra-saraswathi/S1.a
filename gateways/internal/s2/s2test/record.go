// Package s2test lets a gateway's tests hand over what they would publish, so it can
// be checked against the full telemetry contract v1 schema. Go's standard library has
// no JSON Schema validator; connectors/tests/test_contract.py has one, runs the
// gateways' tests with S2_CONTRACT_OUT set, and validates every line written.
//
// Only tests import this package.
package s2test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/veerendra-saraswathi/S1.a/gateways/internal/s2"
)

var mu sync.Mutex

// packagePath is the test's package directory relative to the module root: go test runs
// each package's tests in its own directory.
func packagePath() string {
	pkg, _ := os.Getwd()
	for dir := pkg; dir != filepath.Dir(dir); dir = filepath.Dir(dir) {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			rel, _ := filepath.Rel(dir, pkg)
			return filepath.ToSlash(rel)
		}
	}
	return filepath.Base(pkg)
}

// Record appends each message to $S2_CONTRACT_OUT/<package>.jsonl as
// {"test", "topic", "payload"}, and does nothing when the variable is unset.
func Record(t testing.TB, out ...s2.Outbound) {
	t.Helper()
	dir := os.Getenv("S2_CONTRACT_OUT")
	if dir == "" || len(out) == 0 {
		return
	}
	mu.Lock()
	defer mu.Unlock()
	name := strings.ReplaceAll(packagePath(), "/", "-") // e.g. mavlink-gateway-internal-telemetry
	f, err := os.OpenFile(filepath.Join(dir, name+".jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	for _, o := range out {
		line := struct {
			Test    string     `json:"test"`
			Topic   string     `json:"topic"`
			Payload s2.Message `json:"payload"`
		}{t.Name(), o.Topic, o.Message}
		if err := enc.Encode(line); err != nil {
			t.Fatal(err)
		}
	}
}
